"""Zoho Cliq: OAuth (self-client refresh grant), API client, extraction phase.

IMPORTANT — verify the endpoint constants below against your data centre's live
API docs before a production run. Zoho renames and re-versions these; they are
deliberately centralised here so a correction is a one-line change.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from .http import ApiError, HttpClient
from .state import State

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Endpoint constants — VERIFY THESE
# --------------------------------------------------------------------------
# Verified against cliq.zoho.in on 2026-07-30 via `probe`.
EP = {
    "channels":         "/api/v2/channels",
    "channel_detail":   "/api/v2/channels/{unique_name}",
    "channel_members":  "/api/v2/channels/{unique_name}/members",
    "channel_messages": "/api/v2/channels/{unique_name}/messages",
    "chats":            "/api/v2/chats",
    "chat_detail":      "/api/v2/chats/{chat_id}",
    "chat_messages":    "/api/v2/chats/{chat_id}/messages",
    "file_download":    "/api/v2/files/{file_id}",
    "users":            "/api/v2/users",
}

# No org-wide user endpoint exists on v2 (/organisation/users is a 404, not a
# 403). Probe walks these; users are otherwise derived from message senders.
USER_EP_CANDIDATES = [
    "/api/v2/users",
    "/api/v2/organization/users",
    "/api/v2/buddies",
    "/api/v2/users/me",
]

DC_HOSTS = {
    "com":    ("https://cliq.zoho.com",     "https://accounts.zoho.com"),
    "eu":     ("https://cliq.zoho.eu",      "https://accounts.zoho.eu"),
    "in":     ("https://cliq.zoho.in",      "https://accounts.zoho.in"),
    "com.au": ("https://cliq.zoho.com.au",  "https://accounts.zoho.com.au"),
    "jp":     ("https://cliq.zoho.jp",      "https://accounts.zoho.jp"),
    "ca":     ("https://cliq.zohocloud.ca", "https://accounts.zohocloud.ca"),
}


def as_int(v: Any, default: int = 0) -> int:
    """Cliq returns numerics as strings inconsistently (total_message_count and
    participant_count are strings; creation_time is an int). Never compare a
    raw API value numerically without going through this."""
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


class ZohoAuth:
    """Self-client refresh-token grant. Caches the access token until near expiry."""

    def __init__(self, dc: str):
        self.api_host, self.accounts_host = DC_HOSTS[dc]
        self.client_id = os.environ["ZOHO_CLIENT_ID"]
        self.client_secret = os.environ["ZOHO_CLIENT_SECRET"]
        self.refresh_token = os.environ["ZOHO_REFRESH_TOKEN"]
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def header(self) -> dict[str, str]:
        with self._lock:
            if not self._token or time.time() > self._expires_at - 120:
                self._refresh()
            return {"Authorization": f"Zoho-oauthtoken {self._token}"}

    def _refresh(self) -> None:
        import requests

        r = requests.post(
            f"{self.accounts_host}/oauth/v2/token",
            data={
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
            },
            timeout=60,
        )
        r.raise_for_status()
        body = r.json()
        if "access_token" not in body:
            raise RuntimeError(f"Zoho token refresh failed: {body}")
        self._token = body["access_token"]
        self._expires_at = time.time() + int(body.get("expires_in", 3600))
        log.info("Zoho access token refreshed")


class ZohoClient:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.auth = ZohoAuth(cfg["zoho"]["dc"])
        self.page_size = cfg["zoho"].get("page_size", 100)
        self.http = HttpClient(
            base_url=self.auth.api_host,
            auth_header=self.auth.header,
            rate_per_sec=cfg["zoho"].get("rate_limit_rps", 5.0),
        )

    # ---- pagination -------------------------------------------------------

    @staticmethod
    def extract_items(body: Any, data_key: str = "data") -> list[dict[str, Any]]:
        """Cliq wraps list results under different keys per endpoint, and
        occasionally returns a bare array. Try the declared key, then the known
        ones, then fall back to the first list-of-dicts value in the body."""
        if isinstance(body, list):
            return body
        if not isinstance(body, dict):
            return []
        for key in (data_key, "data", "channels", "chats", "messages",
                    "users", "members", "buddies"):
            v = body.get(key)
            if isinstance(v, list):
                return v
        for v in body.values():
            if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                return v
        return []

    def paginate(self, path: str, params: dict[str, Any] | None = None,
                 data_key: str = "data") -> Iterator[dict[str, Any]]:
        """Cliq v2 rejects unknown query params outright, so the first request
        must carry nothing but `limit`. Pagination style is then detected from
        the response: a `next_token` means token paging, otherwise we probe
        `offset` and degrade gracefully if the endpoint refuses it."""
        params = dict(params or {})
        params.setdefault("limit", self.page_size)
        limit = params["limit"]

        next_token: str | None = None
        offset = 0
        offset_supported = True
        page = 0

        while True:
            q = dict(params)
            if next_token:
                q["next_token"] = next_token
            elif offset:
                q["offset"] = offset

            try:
                body = self.http.get(path, params=q)
            except ApiError as e:
                # endpoint doesn't accept `offset` -> we cannot page further
                if e.status == 400 and "extra_param_found" in e.body and offset:
                    log.warning(
                        "%s rejects 'offset'; stopping after %d items. If this "
                        "endpoint has more data, find its paging param and add it.",
                        path, offset,
                    )
                    return
                raise

            items = self.extract_items(body, data_key)
            if not items:
                return
            for item in items:
                yield item
            page += 1

            has_more = body.get("has_more") if isinstance(body, dict) else None
            next_token = (body.get("next_token") or body.get("sync_token")) \
                if isinstance(body, dict) else None

            if has_more is False:
                return
            if next_token:
                continue
            # Endpoints like /chats ignore `limit` and return everything at
            # once with no token — a short page there is the whole list.
            if len(items) < limit:
                return
            if not offset_supported:
                return
            offset += len(items)

    # ---- typed reads ------------------------------------------------------

    def users(self) -> Iterator[dict[str, Any]]:
        yield from self.paginate(EP["users"])

    def channels(self) -> Iterator[dict[str, Any]]:
        yield from self.paginate(EP["channels"])

    def chats(self) -> Iterator[dict[str, Any]]:
        yield from self.paginate(EP["chats"])

    def _message_path(self, chat_id: Any, unique_name: str | None) -> str | None:
        """Channels the user has never opened report chat_id=null and must be
        addressed by unique_name through the channel endpoint instead."""
        cid = str(chat_id or "").strip()
        if cid and cid.lower() not in ("none", "null", ""):
            return EP["chat_messages"].format(chat_id=cid)
        if unique_name:
            return EP["channel_messages"].format(unique_name=unique_name)
        return None

    def messages(self, chat_id: str, from_ts: int | None = None,
                 unique_name: str | None = None) -> Iterator[dict[str, Any]]:
        """Exhaustive message retrieval.

        The messages endpoint returns bare {"data": [...]} with no next_token
        and no has_more, so token pagination cannot work here. Cliq windows by
        time instead: we walk backwards, repeatedly asking for everything older
        than the oldest message seen so far, until a page comes back empty.

        Dedupes by message id because window boundaries can overlap, and stops
        on no-progress so a misbehaving endpoint cannot spin forever.
        """
        path = self._message_path(chat_id, unique_name)
        if path is None:
            log.error("chat %s: no usable chat_id or unique_name; SKIPPED", chat_id)
            return
        limit = self.page_size
        alt_tried = False
        seen: set[str] = set()
        totime: int | None = None
        guard = 0

        while True:
            guard += 1
            if guard > 10_000:
                log.error("chat %s: pagination guard tripped at %d pages", chat_id, guard)
                return

            params: dict[str, Any] = {"limit": limit}
            if totime is not None:
                params["totime"] = totime
            if from_ts:
                params["fromtime"] = from_ts

            try:
                body = self.http.get(path, params=params)
            except ApiError as e:
                if (e.status == 400 and not alt_tried and unique_name
                        and "chats/" in path):
                    alt_tried = True
                    path = EP["channel_messages"].format(unique_name=unique_name)
                    log.info("chat %s: falling back to channel endpoint (%s)",
                             chat_id, unique_name)
                    continue
                if e.status == 400 and "extra_param_found" in e.body and totime is not None:
                    log.error(
                        "chat %s: endpoint rejects 'totime'; retrieved %d messages "
                        "and CANNOT page further. Data is INCOMPLETE.",
                        chat_id, len(seen),
                    )
                    return
                raise

            items = self.extract_items(body, "data")
            if not items:
                return

            fresh = [m for m in items if str(m.get("id")) not in seen]
            if not fresh:
                return                      # window produced nothing new

            for m in fresh:
                seen.add(str(m.get("id")))
                yield m

            times = [as_int(m.get("time")) for m in items if m.get("time")]
            if not times:
                return
            oldest = min(times)
            if totime is not None and oldest >= totime:
                return                      # not moving backwards
            totime = oldest - 1

            if len(items) < limit:
                return                      # short page = start of history

    def download_file(self, file_id: str, dest: Path) -> tuple[int, str]:
        """Stream a file to disk. Returns (bytes, sha256)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256()
        total = 0
        resp = self.http.get_raw(EP["file_download"].format(file_id=file_id), stream=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                if not chunk:
                    continue
                fh.write(chunk)
                h.update(chunk)
                total += len(chunk)
        tmp.rename(dest)
        return total, h.hexdigest()


# --------------------------------------------------------------------------
# Normalisation — collapse Cliq's many message shapes into one flat record
# --------------------------------------------------------------------------

def normalize_message(raw: dict[str, Any]) -> dict[str, Any]:
    """Cliq messages vary by `type`: text, attachment, file, share, system,
    bot card, form, table. Everything not representable in Teams gets flattened
    to text so nothing is silently lost."""
    content = raw.get("content") or {}
    sender = raw.get("sender") or {}
    mtype = raw.get("type") or "text"

    text = content.get("text") or content.get("comment") or ""
    attachments: list[dict[str, Any]] = []

    # single file
    f = content.get("file")
    if isinstance(f, dict):
        fid = f.get("id") or f.get("file_id") or f.get("fileId")
        if fid:
            attachments.append({
                "id": str(fid),
                "name": f.get("name") or f.get("file_name") or str(fid),
                "size": as_int(f.get("size")),
                "content_type": f.get("content_type") or f.get("type"),
            })

    # multiple files
    for f in content.get("files") or []:
        if isinstance(f, dict) and f.get("id"):
            attachments.append({
                "id": f["id"],
                "name": f.get("name") or f["id"],
                "size": f.get("size"),
            })

    # non-representable payloads -> readable text fallback
    if mtype in ("card", "form", "table", "widget") and not text:
        text = f"[{mtype} from Zoho Cliq]\n" + _flatten(content)

    # deleted -> tombstone, info -> system notice. Both are stored so counts
    # reconcile against Zoho's total, but neither is importable as a message.
    importable = mtype not in ("deleted", "info")
    if mtype == "deleted":
        text = "[message deleted in Zoho Cliq]"
    elif mtype == "info" and not text:
        text = "[system notice]"

    return {
        "id": str(raw.get("id") or raw.get("time")),
        "type": mtype,
        "importable": importable,
        "ts": as_int(raw.get("time") or raw.get("timestamp")),
        "author_id": str(sender.get("id") or ""),
        "author_name": sender.get("name") or "",
        "text": text,
        "attachments": attachments,
        "parent_id": str(raw.get("parent_id") or raw.get("thread_id") or "") or None,
        "edited": bool(content.get("edited")),
        "edited_time": as_int(content.get("edited_time")) or None,
        "revision": as_int(raw.get("revision")),
        "pinned": bool(raw.get("is_pinned")),
        "_raw_type_keys": sorted(content.keys()),  # aids debugging unknown shapes
    }


def _flatten(obj: Any, depth: int = 0) -> str:
    if depth > 4:
        return "..."
    if isinstance(obj, dict):
        return "\n".join(f"{k}: {_flatten(v, depth + 1)}" for k, v in obj.items())
    if isinstance(obj, list):
        return "\n".join(_flatten(v, depth + 1) for v in obj)
    return str(obj)


# --------------------------------------------------------------------------
# Extraction phases
# --------------------------------------------------------------------------

def probe(cli: ZohoClient, name: str, out_dir: Path, **fmt: Any) -> Any:
    """Fetch one page of an endpoint raw and dump it. Use this to discover the
    real response shape before trusting any field name in this module."""
    import json as _json

    out_dir.mkdir(parents=True, exist_ok=True)

    # `users` has no confirmed path — walk the candidates until one answers.
    if name == "users":
        for cand in USER_EP_CANDIDATES:
            try:
                body = cli.http.get(cand, params={"limit": 2})
            except ApiError as e:
                print(f"    {cand:<36} -> {e.status}")
                continue
            print(f"\n=== users  ->  {cand}  [WORKS]")
            _report(body, out_dir / "probe_users.json")
            return body
        print("\n=== users  ->  no candidate worked; will derive from message senders")
        return None

    # `messages` needs a real chat to point at
    if name == "messages":
        src = cli.http.get(EP["channels"], params={"limit": 5})
        chats = ZohoClient.extract_items(src)
        chats = [c for c in chats if as_int(c.get("total_message_count")) > 0] or chats
        if not chats:
            print("\n=== messages  ->  no channel with messages to sample")
            return None
        chat = chats[0]
        cid = chat["chat_id"]
        print(f"\n=== messages  ->  sampling '{chat.get('name')}' "
              f"(chat_id={cid}, zoho reports {chat.get('total_message_count')} msgs)")
        body = cli.http.get(EP["chat_messages"].format(chat_id=cid), params={"limit": 3})
        _report(body, out_dir / "probe_messages.json", deep=True)
        return body

    # `types` sweeps real history to find one example of every message shape.
    # Attachment handling cannot be written correctly without this.
    if name == "types":
        import json as _json

        chans = ZohoClient.extract_items(cli.http.get(EP["channels"], params={"limit": 50}))
        dms = ZohoClient.extract_items(cli.http.get(EP["chats"], params={"limit": 50}))
        targets = [(c.get("name"), c.get("chat_id"), c.get("unique_name"))
                   for c in chans + dms]

        samples: dict[str, dict[str, Any]] = {}
        scanned = 0
        for label, cid, uname in targets:
            try:
                for m in cli.messages(cid, unique_name=uname):
                    scanned += 1
                    t = str(m.get("type") or "unknown")
                    if t not in samples:
                        samples[t] = {"_chat": label, **m}
                    if scanned > 4000:
                        break
            except ApiError as e:
                log.warning("skip %s: %s", label, e)
            if scanned > 4000:
                break

        dest = out_dir / "probe_types.json"
        dest.write_text(_json.dumps(samples, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n=== types  ->  scanned {scanned} messages across {len(targets)} chats")
        for t, ex in sorted(samples.items()):
            ck = sorted((ex.get("content") or {}).keys())
            print(f"  type={t:<14} content keys: {ck}")
        print(f"\nraw written to : {dest}")
        if not any(k in ("file", "attachment", "image", "audio", "video")
                   for k in samples):
            print("\n!! no attachment-bearing message found — share a file in Cliq\n"
                  "   and re-run, or file extraction stays unverified.")
        return samples

    path = EP[name].format(**fmt)
    body = cli.http.get(path, params={"limit": 2})
    print(f"\n=== {name}  ->  {path}")
    _report(body, out_dir / f"probe_{name}.json")
    return body


def _report(body: Any, dest: Path, deep: bool = False) -> None:
    import json as _json

    dest.write_text(_json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
    items = ZohoClient.extract_items(body)
    if isinstance(body, dict):
        print(f"top-level keys : {sorted(body.keys())}")
    print(f"items found    : {len(items)}")
    if items:
        print(f"item keys      : {sorted(items[0].keys())}")
        if deep:
            # message bodies nest the parts that actually matter
            for k in ("sender", "content", "meta", "file"):
                v = items[0].get(k)
                if isinstance(v, dict):
                    print(f"  .{k} keys    : {sorted(v.keys())}")
                elif v is not None:
                    print(f"  .{k}         : {type(v).__name__} = {str(v)[:80]}")
            print("\n  --- first message verbatim ---")
            print(_json.dumps(items[0], indent=2, ensure_ascii=False)[:1400])
    print(f"raw written to : {dest}")


def extract_users(cli: ZohoClient, st: State) -> int:
    """Org endpoint needs admin rights; fall back to deriving users from the
    senders of already-extracted messages when it 403s."""
    n = 0
    try:
        with st.tx():
            for u in cli.users():
                st.upsert_user(
                    str(u.get("id") or u.get("zuid")),
                    u.get("email") or u.get("email_id"),
                    u.get("display_name") or u.get("name"),
                )
                n += 1
        st.log("extract-users", "info", f"{n} users from org endpoint")
        return n
    except ApiError as e:
        if e.status not in (401, 403, 404):
            raise
        log.warning("org user endpoint unavailable (%s); deriving from messages", e.status)
        return extract_users_from_messages(st)


def extract_users_from_messages(st: State) -> int:
    """Non-admin fallback. Yields display names but no emails, so user mapping
    will need mapping/users.csv."""
    import json as _json

    rows = st.rows(
        "SELECT DISTINCT author_zoho_id, payload FROM messages "
        "WHERE author_zoho_id IS NOT NULL AND author_zoho_id != ''"
    )
    seen: set[str] = set()
    with st.tx():
        for r in rows:
            uid = r["author_zoho_id"]
            if uid in seen:
                continue
            seen.add(uid)
            st.upsert_user(uid, None, _json.loads(r["payload"]).get("author_name"))
    st.log("extract-users", "info", f"{len(seen)} users derived from messages")
    return len(seen)


def extract_chats(cli: ZohoClient, st: State) -> int:
    include = set(cli.cfg["zoho"].get("include_channels") or [])
    exclude = set(cli.cfg["zoho"].get("exclude_channels") or [])
    n = 0
    with st.tx():
        for ch in cli.channels():
            name = ch.get("name") or ch.get("unique_name") or ""
            if include and name not in include:
                continue
            if name in exclude:
                continue
            st.upsert_chat(
                str(ch.get("chat_id") or ch.get("channel_id") or ch.get("unique_name")),
                "channel",
                name,
                ch.get("unique_name"),
                as_int(ch.get("participant_count")),
            )
            # Zoho's own count -> lets extraction verify it got everything
            st.db.execute(
                "UPDATE chats SET msg_count_reported=? WHERE zoho_chat_id=?",
                (as_int(ch.get("total_message_count")),
                 str(ch.get("chat_id") or ch.get("channel_id") or ch.get("unique_name"))),
            )
            n += 1

        # DMs and group chats — recorded so they can be HTML-exported later
        for c in cli.chats():
            if c.get("removed"):
                continue
            ctype = (c.get("chat_type") or "").lower()
            if "channel" in ctype:
                continue                     # already captured above
            kind = "group" if as_int(c.get("participant_count")) > 2 else "dm"
            st.upsert_chat(str(c["chat_id"]), kind, c.get("name"),
                           None, as_int(c.get("participant_count")))
            n += 1
    st.log("extract-chats", "info", f"{n} chats")
    return n


def extract_messages(cli: ZohoClient, st: State) -> int:
    total = 0
    chats = st.rows("SELECT * FROM chats WHERE status != 'extracted'")
    for chat in chats:
        cid = chat["zoho_chat_id"]
        # resume: continue from the newest message already stored
        row = st.one("SELECT MAX(ts) m FROM messages WHERE zoho_chat_id=?", (cid,))
        from_ts = (row["m"] + 1) if row and row["m"] else None

        count = 0
        first_ts = chat["first_msg_ts"]
        last_ts = chat["last_msg_ts"]
        try:
            with st.tx():
                for raw in cli.messages(cid, from_ts, chat["unique_name"]):
                    m = normalize_message(raw)
                    if not m["ts"]:
                        continue
                    st.upsert_message(
                        m["id"], cid, m["ts"], m["author_id"], m, m["parent_id"]
                    )
                    for a in m["attachments"]:
                        st.upsert_file(a["id"], cid, m["id"], a["name"], a.get("size"))
                    first_ts = m["ts"] if first_ts is None else min(first_ts, m["ts"])
                    last_ts = m["ts"] if last_ts is None else max(last_ts, m["ts"])
                    count += 1
                st.mark(
                    "chats", "zoho_chat_id", cid, "extracted",
                    first_msg_ts=first_ts, last_msg_ts=last_ts,
                    msg_count_src=(chat["msg_count_src"] or 0) + count,
                )
        except ApiError as e:
            st.mark("chats", "zoho_chat_id", cid, "failed", note=str(e))
            log.error("chat %s failed: %s", cid, e)
            continue

        total += count
        got = st.one("SELECT COUNT(*) n FROM messages WHERE zoho_chat_id=?", (cid,))["n"]
        expected = chat["msg_count_reported"] or 0
        if expected and got < expected:
            log.error("chat %-30s INCOMPLETE: %d/%d messages retrieved",
                      chat["title"], got, expected)
            st.db.execute(
                "UPDATE chats SET note=? WHERE zoho_chat_id=?",
                (f"INCOMPLETE {got}/{expected}", cid))
        else:
            log.info("chat %-30s %d messages (zoho reports %s)",
                     chat["title"], got, expected or "?")
    return total


def extract_files(cli: ZohoClient, st: State, blob_dir: Path) -> int:
    pending = st.rows("SELECT * FROM files WHERE status NOT IN ('done','skipped')")
    ok = 0
    for f in pending:
        fid = f["zoho_file_id"]
        safe = "".join(c for c in (f["name"] or fid) if c.isalnum() or c in "._- ")[:120]
        dest = blob_dir / f["zoho_chat_id"] / f"{fid}_{safe}"
        st.bump_attempts("files", "zoho_file_id", fid)
        try:
            size, digest = cli.download_file(fid, dest)
            st.mark("files", "zoho_file_id", fid, "done",
                    local_path=str(dest), sha256=digest, size=size)
            ok += 1
        except ApiError as e:
            st.mark("files", "zoho_file_id", fid, "failed", error=str(e))
            log.warning("file %s failed: %s", fid, e)
    return ok