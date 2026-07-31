"""DM and group-chat load: Cliq 1:1 / group chats -> Teams chats.

This is the counterpart to the channel loader in graph.py. Teams treats chats
differently from channels in three ways that shape everything here:

  1. A chat cannot be created in migration mode. You create (or resolve) it,
     then call startMigration with a backdated conversationCreationDateTime.
  2. There is no channel files folder. Attachments must already live in
     SharePoint or OneDrive and are attached by reference, so blobs go to the
     owner's OneDrive with an organization-scoped sharing link.
  3. createdDateTime must be unique to the millisecond within the chat, and
     later than the chat's own createdDateTime. A 409 is retried with the
     timestamp nudged forward by 1 ms.

Ordering rule, same shape as channels:
    chat.conversationCreationDateTime <= min(message.createdDateTime)

Every step is idempotent and recorded in state.db, so a killed run resumes.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .http import ApiError
from .state import State
from .transform import backdate, build_message_payload, render_body

log = logging.getLogger(__name__)

MAX_409_RETRIES = 5


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def importable_count(st: State, chat_id: str) -> int:
    """Messages that will actually be posted. `deleted` and `info` rows are kept
    for count reconciliation but are not importable."""
    return st.one(
        "SELECT COUNT(*) n FROM messages WHERE zoho_chat_id=?"
        " AND COALESCE(json_extract(payload,'$.importable'), 1) != 0",
        (chat_id,))["n"]


def _pending(st: State, chat_id: str, limit: int = 200) -> list[Any]:
    """Oldest first. Unlike State.pending_messages this treats 'failed' as
    terminal — otherwise a row that exhausts its attempts is re-selected
    forever and the batch loop never drains."""
    return st.rows(
        """SELECT * FROM messages
           WHERE zoho_chat_id=? AND status NOT IN ('done','skipped','failed')
           ORDER BY ts ASC, zoho_msg_id ASC LIMIT ?""",
        (chat_id, limit),
    )


def owner_identity(gc: Any, st: State, cfg: dict[str, Any]) -> dict[str, Any]:
    upn = cfg["dms"].get("owner_upn")
    if not upn:
        raise RuntimeError("set dms.owner_upn — the account the DMs were exported from")
    u = gc.find_user(upn)
    if not u:
        raise RuntimeError(f"dms.owner_upn {upn} not found in the tenant")
    return u


def resolve_participants(st: State, chat: Any, owner_aad_id: str) -> tuple[list[str], list[str]]:
    """Who belongs in this chat, as AAD ids.

    Cliq's chat list never gave us participant rosters, so membership is
    reconstructed from who actually spoke, plus a display-name match on the
    chat title (which covers a 1:1 where the other party never replied).
    Returns (aad_ids, problems)."""
    problems: list[str] = []
    ids: dict[str, None] = {owner_aad_id: None}

    for r in st.rows(
        """SELECT DISTINCT m.author_zoho_id z FROM messages m
           WHERE m.zoho_chat_id=?""", (chat["zoho_chat_id"],)
    ):
        u = st.one("SELECT * FROM users WHERE zoho_id=? AND status='mapped'", (r["z"],))
        if u and u["aad_id"]:
            ids[u["aad_id"]] = None
        else:
            who = st.one("SELECT display_name FROM users WHERE zoho_id=?", (r["z"],))
            problems.append(
                f"author {r['z']}"
                + (f" ({who['display_name']})" if who and who["display_name"] else "")
                + " unmapped -> attributed to the orphan identity"
            )

    # a 1:1 where only the owner spoke still needs the counterpart
    title_hit = st.one(
        "SELECT * FROM users WHERE status='mapped' AND aad_id IS NOT NULL"
        " AND replace(replace(lower(display_name),' ',''),'.','') = ?",
        (_norm(chat["title"]),),
    )
    if title_hit:
        ids[title_hit["aad_id"]] = None

    return list(ids), problems


def plan_dms(st: State, cfg: dict[str, Any], gc: Any) -> list[dict[str, Any]]:
    """Dry run. Resolves every chat exactly as the loader would and reports what
    it would create, without touching the tenant beyond directory reads."""
    owner = owner_identity(gc, st, cfg)
    headroom = cfg["teams"]["backdate_headroom_hours"]
    kinds = ("dm", "group") if cfg["dms"].get("include_groups") else ("dm",)

    out = []
    for chat in st.rows(
        f"SELECT * FROM chats WHERE kind IN ({','.join('?' * len(kinds))})"
        " AND msg_count_src > 0 ORDER BY msg_count_src DESC", kinds
    ):
        ids, problems = resolve_participants(st, chat, owner["id"])
        want = "group" if chat["kind"] == "group" else "oneOnOne"
        blocked = None
        if want == "oneOnOne" and len(ids) != 2:
            blocked = (f"needs exactly 2 participants, resolved {len(ids)} "
                       f"— no Teams 1:1 chat can represent this")
        elif want == "group" and len(ids) < 3:
            blocked = f"group chat with only {len(ids)} resolved participants"

        importable = importable_count(st, chat["zoho_chat_id"])
        if not blocked and not importable:
            blocked = "nothing importable (every message is a tombstone)"
        files = st.rows(
            "SELECT COALESCE(SUM(size),0) b, COUNT(*) n FROM files"
            " WHERE zoho_chat_id=? AND status='done'", (chat["zoho_chat_id"],))[0]

        out.append({
            "title": chat["title"],
            "zoho_chat_id": chat["zoho_chat_id"],
            "kind": chat["kind"],
            "chat_type": want,
            "participants": len(ids),
            "messages_total": chat["msg_count_src"],
            "messages_importable": importable,
            "files": files["n"],
            "file_bytes": files["b"],
            "conversation_created": backdate(chat["first_msg_ts"], headroom),
            "teams_chat_id": chat["teams_chat_id"],
            "blocked": blocked,
            "notes": problems,
        })
    return out


def _upload_name(name: str | None, fallback_id: str) -> str:
    """Teams rejects an attachment whose URL has no file extension, so a blob
    that arrived from Cliq without one gets `.bin`. Six DM files are affected —
    all Go binaries."""
    n = (name or "").strip() or fallback_id
    return n if "." in n.rsplit("/", 1)[-1] else n + ".bin"


def _drive(gc: Any, cfg: dict[str, Any], chat: Any, owner_id: str,
           cache: dict[str, Any]) -> tuple[str, str, str]:
    """(drive_id, drive_web_url, folder_id), resolved once per chat."""
    if "drive_id" not in cache:
        cache["drive_id"], cache["drive_web_url"] = gc.user_drive(owner_id)
    if "folder_id" not in cache:
        cache["folder_id"] = gc.ensure_folder(
            cache["drive_id"],
            [cfg["dms"].get("onedrive_folder") or "Cliq Archive",
             chat["title"] or chat["zoho_chat_id"]],
        )
    return cache["drive_id"], cache["drive_web_url"], cache["folder_id"]


def _upload_attachments(gc: Any, st: State, cfg: dict[str, Any], chat: Any,
                        owner_id: str, files: list[Any],
                        cache: dict[str, Any]) -> list[dict[str, Any]]:
    """Push blobs to the owner's OneDrive and return attachment descriptors.
    Already-uploaded files are reused from state, so a resumed run re-uploads
    nothing."""
    if cfg["dms"].get("attachments") != "link":
        return []

    limit_mb = cfg["dms"].get("max_attachment_mb") or 0
    uploaded = []
    for f in files:
        if f["sp_etag_guid"]:
            row = dict(f)
            # a row uploaded before the contentUrl fix carries an Office
            # Doc.aspx URL; rebuild it from the item, without re-uploading
            if "/_layouts/" in (row["sp_web_url"] or "") or gc.needs_extension(
                    (row["sp_web_url"] or "").split("?")[0]):
                drive_id, drive_url, _ = _drive(gc, cfg, chat, owner_id, cache)
                item = gc.get_drive_item(drive_id, row["sp_item_id"])
                row["sp_web_url"] = gc.item_path_url(drive_url, item)
                row["name"] = item["name"]
                st.mark("files", "zoho_file_id", f["zoho_file_id"], "done",
                        sp_web_url=row["sp_web_url"], name=row["name"])
                log.info("repaired contentUrl for %s", row["name"])
            uploaded.append(row)
            continue
        if limit_mb and (f["size"] or 0) > limit_mb * 1024 * 1024:
            log.warning("skipping %s (%.1f MB > max_attachment_mb)",
                        f["name"], (f["size"] or 0) / 1048576)
            st.mark("files", "zoho_file_id", f["zoho_file_id"], "skipped",
                    error="over max_attachment_mb")
            continue
        path = Path(f["local_path"] or "")
        if not path.exists():
            log.warning("blob missing on disk: %s", f["local_path"])
            continue

        drive_id, drive_url, folder_id = _drive(gc, cfg, chat, owner_id, cache)
        name = _upload_name(f["name"], f["zoho_file_id"])
        # a multi-hundred-MB upload logs nothing for minutes otherwise
        log.info("  uploading %s (%.1f MB)", name, (f["size"] or 0) / 1048576)
        item = gc.upload_file(drive_id, folder_id, name, path)
        guid = gc.etag_guid(item)
        content_url = gc.item_path_url(drive_url, item)
        share = gc.create_org_link(drive_id, item["id"])
        st.mark("files", "zoho_file_id", f["zoho_file_id"], "done",
                name=item["name"], sp_item_id=item["id"], sp_etag_guid=guid,
                sp_web_url=content_url, sp_share_url=share)
        uploaded.append({**dict(f), "name": item["name"], "sp_etag_guid": guid,
                         "sp_web_url": content_url})
    return uploaded


def load_dms(gc: Any, st: State, cfg: dict[str, Any],
             only: str | None = None, limit_chats: int | None = None) -> int:
    """Create the chats, start migration, import every message in timestamp
    order, and leave the chat in migration mode for `complete-dms`."""
    owner = owner_identity(gc, st, cfg)
    orphan_upn = cfg["mapping"].get("orphan_author_upn")
    orphan = gc.find_user(orphan_upn) if orphan_upn else None
    headroom = cfg["teams"]["backdate_headroom_hours"]
    kinds = ("dm", "group") if cfg["dms"].get("include_groups") else ("dm",)

    chats = st.rows(
        f"SELECT * FROM chats WHERE kind IN ({','.join('?' * len(kinds))})"
        " AND msg_count_src > 0 ORDER BY msg_count_src ASC", kinds
    )
    if only:
        chats = [c for c in chats
                 if only in (c["zoho_chat_id"], c["title"] or "")]
        if not chats:
            raise RuntimeError(f"no DM/group chat matching {only!r}")
    if limit_chats:
        chats = chats[:limit_chats]

    total = 0
    for chat in chats:
        cid = chat["zoho_chat_id"]
        ids, problems = resolve_participants(st, chat, owner["id"])
        want = "group" if chat["kind"] == "group" else "oneOnOne"
        if want == "oneOnOne" and len(ids) != 2:
            log.error("skipping %s: resolved %d participants, need exactly 2",
                      chat["title"], len(ids))
            st.mark("chats", "zoho_chat_id", cid, chat["status"],
                    note=f"dm skipped: {len(ids)} participants resolved")
            continue
        if want == "group" and len(ids) < 3:
            log.error("skipping group %s: only %d participants resolved — a Teams "
                      "group chat needs 3", chat["title"], len(ids))
            st.mark("chats", "zoho_chat_id", cid, chat["status"],
                    note=f"group skipped: {len(ids)} participants resolved")
            continue
        if not importable_count(st, cid):
            log.info("skipping %s: nothing importable", chat["title"])
            st.mark("chats", "zoho_chat_id", cid, chat["status"],
                    note="dm skipped: nothing importable")
            continue
        for p in problems:
            log.info("%s: %s", chat["title"], p)

        # 1. the chat itself. A chat that can't be created or put into
        # migration mode is recorded and stepped over — one bad chat must not
        # abort a multi-hour unattended run.
        try:
            teams_chat_id = chat["teams_chat_id"]
            if not teams_chat_id:
                teams_chat_id = gc.create_or_get_chat(ids, want, chat["title"])
                st.mark("chats", "zoho_chat_id", cid, chat["status"],
                        teams_chat_id=teams_chat_id)
                log.info("%s -> chat %s (%s, %d members)",
                         chat["title"], teams_chat_id, want, len(ids))

            # 2. migration mode, backdated ahead of the earliest message
            if chat["chat_migration"] not in ("started", "completed"):
                created = backdate(chat["first_msg_ts"], headroom)
                gc.start_chat_migration(teams_chat_id, created)
                st.mark("chats", "zoho_chat_id", cid, chat["status"],
                        chat_migration="started")
                log.info("%s: migration mode from %s", chat["title"], created)
        except ApiError as e:
            log.error("%s: cannot prepare chat, skipping: %s", chat["title"], e)
            st.mark("chats", "zoho_chat_id", cid, chat["status"],
                    note=f"chat setup failed: {str(e)[:400]}")
            continue

        # 3. messages, oldest first
        cache: dict[str, Any] = {}
        chat_done = st.one("SELECT COUNT(*) n FROM messages WHERE zoho_chat_id=?"
                           " AND status='done'", (cid,))["n"]
        remaining = importable_count(st, cid) - chat_done
        if remaining > 0:
            log.info("%s: importing %d messages (%d already done)",
                     chat["title"], remaining, chat_done)
        while True:
            batch = _pending(st, cid, limit=200)
            if not batch:
                break
            progressed = False
            for row in batch:
                mid = row["zoho_msg_id"]
                st.bump_attempts("messages", "zoho_msg_id", mid)

                if not json.loads(row["payload"]).get("importable", True):
                    st.mark("messages", "zoho_msg_id", mid, "skipped")
                    progressed = True
                    continue
                if row["attempts"] >= 5:
                    st.mark("messages", "zoho_msg_id", mid, "failed",
                            error="max attempts exceeded")
                    progressed = True
                    continue

                try:
                    files = st.rows(
                        "SELECT * FROM files WHERE zoho_msg_id=?"
                        " AND status IN ('done','pending')", (mid,))
                    uploaded = _upload_attachments(
                        gc, st, cfg, chat, owner["id"], files, cache)

                    ts, new_id = row["ts"], None
                    for attempt in range(MAX_409_RETRIES):
                        payload = build_message_payload(
                            st, row, uploaded, orphan,
                            ts_override=ts if ts != row["ts"] else None)
                        # not linking blobs: keep the filename in the transcript
                        # rather than losing the fact a file was shared
                        if not uploaded:
                            named = json.loads(row["payload"]).get("attachments") or []
                            for a in named:
                                payload["body"]["content"] += (
                                    f'<br><i>&#128206; {a.get("name") or a.get("id")} '
                                    f'(file not migrated)</i>')
                        try:
                            new_id = gc.import_chat_message(teams_chat_id, payload)
                            break
                        except ApiError as e:
                            # createdDateTime must be unique to the millisecond
                            if e.status != 409:
                                raise
                            ts += 1
                            log.info("409 on %s; retrying at +%d ms", mid, ts - row["ts"])
                    if new_id is None:
                        raise ApiError(409, "createdDateTime collision unresolved",
                                       teams_chat_id)

                    st.mark("messages", "zoho_msg_id", mid, "done",
                            teams_msg_id=new_id)
                    total += 1
                    progressed = True
                    if total % 25 == 0:
                        log.info("%s: %d/%d imported (%d this run)",
                                 chat["title"], chat_done + 1, chat["msg_count_src"],
                                 total)
                    chat_done += 1
                except ApiError as e:
                    st.mark("messages", "zoho_msg_id", mid, "pending", error=str(e))
                    log.warning("msg %s failed: %s", mid, e)
            if not progressed:
                log.error("%s: no progress on a batch of %d; moving on",
                          chat["title"], len(batch))
                break

        done = st.one("SELECT COUNT(*) n FROM messages WHERE zoho_chat_id=?"
                      " AND status='done'", (cid,))["n"]
        log.info("%s: %d messages imported", chat["title"], done)
    return total


def complete_dms(gc: Any, st: State, assume_yes: bool = False) -> int:
    """Take every fully-imported chat out of migration mode. Until this runs,
    clients show the import banner and may not display every message."""
    n = 0
    for chat in st.rows(
        "SELECT * FROM chats WHERE teams_chat_id IS NOT NULL AND chat_migration='started'"
    ):
        stuck = st.one(
            "SELECT COUNT(*) n FROM messages WHERE zoho_chat_id=?"
            " AND status NOT IN ('done','skipped')", (chat["zoho_chat_id"],))["n"]
        if stuck:
            log.error("refusing to complete %s: %d messages not imported",
                      chat["title"], stuck)
            continue
        gc.complete_chat_migration(chat["teams_chat_id"])
        st.mark("chats", "zoho_chat_id", chat["zoho_chat_id"], chat["status"],
                chat_migration="completed")
        log.info("%s: migration completed", chat["title"])
        n += 1
    return n


def would_skip(st: State, chat: Any, owner_aad_id: str | None) -> bool:
    """Predict the loader's skip decision without calling Graph, so progress
    totals don't count chats that can never import."""
    if not importable_count(st, chat["zoho_chat_id"]):
        return True
    if not owner_aad_id:
        return False
    ids, _ = resolve_participants(st, chat, owner_aad_id)
    return len(ids) != 2 if chat["kind"] != "group" else len(ids) < 3


def status_dms(st: State, cfg: dict[str, Any]) -> None:
    """Local-only progress. Safe to run while a load is in flight."""
    owner = st.one("SELECT aad_id FROM users WHERE lower(aad_upn)=lower(?)",
                   (cfg["dms"].get("owner_upn") or "",))
    owner_id = owner["aad_id"] if owner else None
    if not owner_id:
        print("(owner_upn not resolved locally — skip prediction unavailable)\n")
    print(f"{'chat':<24}{'kind':<7}{'src':>5}{'done':>6}{'left':>6}{'files':>7}"
          f"{'MB left':>9}  migration")
    tot = {"done": 0, "left": 0, "mb": 0.0}
    for c in st.rows("SELECT * FROM chats WHERE kind IN ('dm','group')"
                     " AND msg_count_src > 0 ORDER BY msg_count_src DESC"):
        cid = c["zoho_chat_id"]
        done = st.one("SELECT COUNT(*) n FROM messages WHERE zoho_chat_id=?"
                      " AND status='done'", (cid,))["n"]
        left = st.one("SELECT COUNT(*) n FROM messages WHERE zoho_chat_id=?"
                      " AND status NOT IN ('done','skipped')", (cid,))["n"]
        f = st.rows("SELECT COUNT(*) n, COALESCE(SUM(size),0) b FROM files"
                    " WHERE zoho_chat_id=? AND sp_etag_guid IS NULL", (cid,))[0]
        note = (c["note"] or "")
        state = c["chat_migration"] or (
            "SKIP" if ("skipped" in note or would_skip(st, c, owner_id)) else "-")
        tot["done"] += done
        if state != "SKIP":
            tot["left"] += left
            tot["mb"] += f["b"] / 1048576
        print(f"{(c['title'] or '')[:23]:<24}{c['kind']:<7}{c['msg_count_src']:>5}"
              f"{done:>6}{left:>6}{f['n']:>7}{f['b'] / 1048576:>9.0f}  {state}")
    print(f"\nimported {tot['done']}, still to import {tot['left']}, "
          f"{tot['mb'] / 1024:.2f} GB left to upload")
    print("'SKIP' chats can never be imported (bot author, user not in tenant, "
          "or nothing but tombstones) — see plan-dms.")


BUNDLE_HTML = """<!doctype html><meta charset="utf-8"><title>{title}</title>
<style>
 body{{font:14px/1.6 -apple-system,Segoe UI,sans-serif;max-width:900px;
   margin:2rem auto;padding:0 1rem;color:#1a1a1a}}
 h1{{font-size:1.3rem;border-bottom:2px solid #ddd;padding-bottom:.5rem}}
 .meta{{color:#666;font-size:.85rem;margin-bottom:2rem}}
 .m{{margin:1rem 0;padding-left:.75rem;border-left:3px solid #eee}}
 .a{{font-weight:600}} .t{{color:#888;font-size:.78rem;margin-left:.5rem}}
 pre{{background:#f6f8fa;padding:.7rem;overflow:auto;border-radius:4px}}
 code{{background:#f6f8fa;padding:.1rem .3rem;border-radius:3px}}
 .att{{margin-top:.4rem}}
 .att a{{display:inline-block;background:#f1f5f9;border:1px solid #cbd5e1;
   border-radius:4px;padding:.25rem .6rem;margin:.15rem .3rem .15rem 0;
   text-decoration:none;color:#0a58ca;font-size:.85rem}}
 .att a:hover{{background:#e2e8f0}}
 .sz{{color:#64748b;font-size:.78rem}}
 @media (prefers-color-scheme:dark){{
   body{{background:#111;color:#e5e5e5}} h1{{border-color:#333}}
   .m{{border-left-color:#333}} pre,code{{background:#1c1c1c}}
   .att a{{background:#1e293b;border-color:#334155;color:#7cb2ff}}
 }}
</style>
<h1>{title}</h1>
<p class=meta>Exported from Zoho Cliq &middot; {count} messages &middot;
{files} attachments ({size})<br>
Attachments are in the <code>files/</code> folder next to this page, so the whole
folder can be zipped or moved and the links keep working.</p>
{body}
"""


def bundle_chat(st: State, cfg: dict[str, Any], selector: str) -> Path:
    """Build a portable folder: one HTML page plus every attachment, linked.

    Unlike the plain transcript this makes the files reachable -- they are hard
    linked where possible so 2 GB of attachments costs no extra disk, while the
    folder still zips as a self-contained unit."""
    chat = find_chat(st, selector)
    cid = chat["zoho_chat_id"]
    title = chat["title"] or cid
    safe_dir = re.sub(r"[^\w\- ]", "_", title).strip()[:60] or cid

    out = Path(cfg["paths"]["report_dir"]).parent / "bundles" / safe_dir
    files_dir = out / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    # copy/link every blob first, so names can be referenced while rendering
    local: dict[str, str] = {}
    used: set[str] = set()
    total_bytes = 0
    for f in st.rows("SELECT * FROM files WHERE zoho_chat_id=?", (cid,)):
        src = Path(f["local_path"] or "")
        if not src.exists():
            continue
        name = re.sub(r'[<>:"/\\|?*]', "_", f["name"] or f["zoho_file_id"])[:120]
        stem, dot, ext = name.rpartition(".")
        n, candidate = 1, name
        while candidate.lower() in used:      # same filename shared twice
            candidate = f"{stem or name}_{n}{dot}{ext}" if dot else f"{name}_{n}"
            n += 1
        used.add(candidate.lower())
        dest = files_dir / candidate
        if not dest.exists():
            try:
                os.link(src, dest)            # no extra disk on the same volume
            except OSError:
                shutil.copy2(src, dest)
        local[f["zoho_file_id"]] = candidate
        total_bytes += f["size"] or 0

    parts = []
    msgs = st.rows("SELECT * FROM messages WHERE zoho_chat_id=? ORDER BY ts", (cid,))
    for m in msgs:
        p = json.loads(m["payload"])
        when = datetime.fromtimestamp(m["ts"] / 1000, tz=timezone.utc)
        atts = ""
        for a in p.get("attachments") or []:
            fname = local.get(a["id"])
            label = html.escape(a.get("name") or a["id"])
            size = f' <span class=sz>{(a.get("size") or 0) / 1048576:.1f} MB</span>' \
                if (a.get("size") or 0) > 1048576 else ""
            atts += (f'<a href="files/{quote(fname)}">&#128206; {label}</a>{size}'
                     if fname else
                     f'<span class=sz>&#128206; {label} (not downloaded)</span>')
        parts.append(
            f'<div class=m><span class=a>'
            f'{html.escape(p.get("author_name") or "?")}</span>'
            f'<span class=t>{when:%Y-%m-%d %H:%M UTC}</span><br>'
            f'{render_body(p.get("text", ""), [])}'
            f'{f"<div class=att>{atts}</div>" if atts else ""}</div>')

    index = out / "index.html"
    index.write_text(BUNDLE_HTML.format(
        title=html.escape(title), count=len(msgs), files=len(local),
        size=f"{total_bytes / 1e9:.2f} GB" if total_bytes > 1e9
             else f"{total_bytes / 1e6:.0f} MB",
        body="".join(parts)), encoding="utf-8")
    log.info("bundle: %d messages, %d attachments (%.2f GB) -> %s",
             len(msgs), len(local), total_bytes / 1e9, index)
    return index


def find_chat(st: State, selector: str) -> Any:
    row = st.one("SELECT * FROM chats WHERE zoho_chat_id=? OR title=?",
                 (selector, selector))
    if row:
        return row
    rows = st.rows("SELECT * FROM chats WHERE title LIKE ?", (f"%{selector}%",))
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise RuntimeError(f"no conversation matching {selector!r}")
    raise RuntimeError(f"{selector!r} matches {len(rows)} conversations: "
                       + ", ".join(r["title"] or r["zoho_chat_id"] for r in rows))


def import_chat_as_channel(gc: Any, st: State, cfg: dict[str, Any],
                           selector: str, channel_name: str | None = None) -> int:
    """Import a DM or group chat into a Teams *channel* instead of a chat.

    This is the escape hatch for conversations Teams cannot represent as a chat
    -- a bot on the other side, or a participant who has left the tenant. A
    channel message does not require its author to be a member of anything, so
    the history survives with real timestamps even when no valid chat exists.

    The parent team has usually had completeMigration called on it already, so
    the channel is created normally and then reopened with startMigration."""
    chat = find_chat(st, selector)
    cid = chat["zoho_chat_id"]
    headroom = cfg["teams"]["backdate_headroom_hours"]

    team = st.one("SELECT * FROM teams_ WHERE teams_team_id IS NOT NULL")
    if not team:
        raise RuntimeError("no Team exists yet -- run load-teams first")
    team_id = team["teams_team_id"]

    orphan_upn = cfg["mapping"].get("orphan_author_upn")
    orphan = gc.find_user(orphan_upn) if orphan_upn else None

    # Reuse teams_channel_id so a resumed run doesn't create a second channel.
    chan_id = chat["teams_channel_id"]
    if not chan_id:
        name = channel_name or f"{chat['title'] or cid} (archive)"
        name = re.sub(r'[#%&*{}/\\:<>?+|"`]', "", name)[:50].rstrip()
        chan_id = gc.create_plain_channel(
            team_id, name, f"Imported from Zoho Cliq: {chat['title']}")
        st.mark("chats", "zoho_chat_id", cid, chat["status"],
                teams_team_id=team_id, teams_channel_id=chan_id)
        log.info("channel %r created", name)

    if chat["chat_migration"] != "channel_started":
        created = backdate(chat["first_msg_ts"], headroom)
        gc.start_channel_migration(team_id, chan_id, created)
        st.mark("chats", "zoho_chat_id", cid, chat["status"],
                chat_migration="channel_started")
        log.info("%s: channel reopened for import from %s", chat["title"], created)

    drive_id = folder_id = drive_url = None
    total = 0
    link_files = cfg["dms"].get("attachments") == "link"
    log.info("%s: importing %d messages into the channel",
             chat["title"], importable_count(st, cid))

    while True:
        batch = _pending(st, cid, limit=200)
        if not batch:
            break
        progressed = False
        for row in batch:
            mid = row["zoho_msg_id"]
            st.bump_attempts("messages", "zoho_msg_id", mid)
            if not json.loads(row["payload"]).get("importable", True):
                st.mark("messages", "zoho_msg_id", mid, "skipped")
                progressed = True
                continue
            if row["attempts"] >= 5:
                st.mark("messages", "zoho_msg_id", mid, "failed",
                        error="max attempts exceeded")
                progressed = True
                continue
            try:
                uploaded = []
                files = st.rows("SELECT * FROM files WHERE zoho_msg_id=? "
                                "AND status IN ('done','pending')", (mid,))
                if files and link_files:
                    if drive_id is None:
                        drive_id, folder_id = gc.channel_files_folder(team_id, chan_id)
                        drive_url = gc.drive_web_url(drive_id)
                    for f in files:
                        if f["sp_etag_guid"]:
                            # NB: not `row` -- that is the message being imported
                            frow = dict(f)
                            # An Office document's webUrl is /_layouts/15/Doc.aspx
                            # with no filename, and Teams reads the file type from
                            # the end of the URL. Rebuild it without re-uploading.
                            if ("/_layouts/" in (frow["sp_web_url"] or "")
                                    or gc.needs_extension(
                                        (frow["sp_web_url"] or "").split("?")[0])):
                                item = gc.get_drive_item(drive_id, frow["sp_item_id"])
                                frow["sp_web_url"] = gc.item_path_url(drive_url, item)
                                frow["name"] = item["name"]
                                st.mark("files", "zoho_file_id", f["zoho_file_id"],
                                        "done", sp_web_url=frow["sp_web_url"],
                                        name=frow["name"])
                                log.info("  repaired link for %s", frow["name"])
                            uploaded.append(frow)
                            continue
                        path = Path(f["local_path"] or "")
                        if not path.exists():
                            log.warning("  blob missing: %s", f["local_path"])
                            continue
                        name = _upload_name(f["name"], f["zoho_file_id"])
                        log.info("  uploading %s (%.1f MB)", name,
                                 (f["size"] or 0) / 1048576)
                        item = gc.upload_file(drive_id, folder_id, name, path)
                        guid = gc.etag_guid(item)
                        content_url = gc.item_path_url(drive_url, item)
                        st.mark("files", "zoho_file_id", f["zoho_file_id"], "done",
                                name=item["name"], sp_item_id=item["id"],
                                sp_etag_guid=guid, sp_web_url=content_url)
                        uploaded.append({**dict(f), "name": item["name"],
                                         "sp_etag_guid": guid,
                                         "sp_web_url": content_url})

                ts, new_id = row["ts"], None
                for _ in range(MAX_409_RETRIES):
                    payload = build_message_payload(
                        st, row, uploaded, orphan,
                        ts_override=ts if ts != row["ts"] else None)
                    try:
                        new_id = gc.import_message(team_id, chan_id, payload, None)
                        break
                    except ApiError as e:
                        if e.status != 409:
                            raise
                        ts += 1
                if new_id is None:
                    raise ApiError(409, "createdDateTime collision", chan_id)

                st.mark("messages", "zoho_msg_id", mid, "done", teams_msg_id=new_id)
                total += 1
                progressed = True
                if total % 25 == 0:
                    log.info("  %s: %d imported", chat["title"], total)
            except ApiError as e:
                st.mark("messages", "zoho_msg_id", mid, "pending", error=str(e))
                log.warning("msg %s failed: %s", mid, e)
        if not progressed:
            log.error("%s: no progress on a batch of %d; stopping",
                      chat["title"], len(batch))
            break

    stuck = st.one("SELECT COUNT(*) n FROM messages WHERE zoho_chat_id=? "
                   "AND status NOT IN ('done','skipped')", (cid,))["n"]
    if stuck:
        log.warning("%s: %d messages outstanding -- channel left in migration "
                    "mode; re-run to finish", chat["title"], stuck)
    else:
        try:
            gc.complete_channel(team_id, chan_id)
            st.mark("chats", "zoho_chat_id", cid, chat["status"],
                    chat_migration="channel_completed")
            log.info("%s: %d messages imported and now visible in the channel",
                     chat["title"], total)
        except ApiError as e:
            log.error("%s: completeMigration failed: %s", chat["title"], e)
    return total


def share_group_history(gc: Any, st: State, cfg: dict[str, Any]) -> int:
    """Make imported messages visible to group chat members.

    A member's visibleHistoryStartDateTime is set when they are added and can
    be neither set at creation nor patched later, so imported messages dated
    before it stay hidden from them. The only remedy Microsoft documents is to
    remove the member and add them back with the value backdated.

    Removing a member is destructive if the re-add then fails, so each failure
    is logged with the exact user id needed to repair it by hand."""
    headroom = cfg["teams"]["backdate_headroom_hours"]
    fixed = 0

    for chat in st.rows("SELECT * FROM chats WHERE kind='group' "
                        "AND teams_chat_id IS NOT NULL"):
        cid = chat["teams_chat_id"]
        # comfortably before the oldest imported message
        since = backdate(chat["first_msg_ts"], headroom * 2)

        try:
            members = gc.list_chat_members(cid)
        except ApiError as e:
            if e.status == 403:
                log.error("cannot read members of %s: the app needs the "
                          "ChatMember.ReadWrite.All application permission",
                          chat["title"])
                return fixed
            raise

        log.info("%s: re-sharing history from %s to %d members",
                 chat["title"], since, len(members))
        for m in members:
            uid = m.get("userId")
            name = m.get("displayName") or uid
            if not uid:
                continue
            if m.get("visibleHistoryStartDateTime", "") <= since:
                log.info("  %s already sees the full history", name)
                continue
            roles = m.get("roles") or []
            try:
                gc.remove_chat_member(cid, m["id"])
            except ApiError as e:
                log.error("  could not remove %s: %s", name, e)
                continue
            try:
                gc.add_chat_member(cid, uid, since, roles)
                log.info("  %s re-added with full history", name)
                fixed += 1
            except ApiError as e:
                log.error("  REMOVED %s BUT COULD NOT RE-ADD THEM: %s\n"
                          "    repair by adding user %s back to chat %s",
                          name, e, uid, cid)
    return fixed


def verify_dms(gc: Any, st: State) -> int:
    """Reconcile source vs destination per chat. Non-zero delta is a failure."""
    rows = st.rows("SELECT * FROM chats WHERE teams_chat_id IS NOT NULL")
    bad = 0
    denied = False
    print(f"{'chat':<34}{'source':>8}{'teams':>8}{'delta':>7}  state")
    for c in rows:
        src = st.one("SELECT COUNT(*) n FROM messages WHERE zoho_chat_id=?"
                     " AND status='done'", (c["zoho_chat_id"],))["n"]
        try:
            dst = gc.count_chat_messages(c["teams_chat_id"])
        except ApiError as e:
            # Teamwork.Migrate.All can write a chat but not read it back
            if e.status == 403:
                denied = True
            print(f"{(c['title'] or '')[:33]:<34}{src:>8}{'403' if e.status == 403 else '?':>8}"
                  f"{'?':>7}  {c['chat_migration'] or 'none'}")
            bad += 1
            continue
        delta = dst - src
        bad += delta < 0
        print(f"{(c['title'] or '')[:33]:<34}{src:>8}{dst:>8}{delta:>7}  "
              f"{c['chat_migration'] or 'none'}")
    print(f"\n{len(rows)} chats, {bad} with a shortfall")
    if denied:
        print("403 = the app can import into a chat but not read it back. Add the "
              "ChatMessage.Read.All application permission (admin consent) to make "
              "this reconciliation possible; import itself does not need it.")
    else:
        print("note: a positive delta is normal — the pair may already have had a "
              "live Teams conversation before the import.")
    return bad
