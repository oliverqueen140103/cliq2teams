"""Microsoft Graph load phase — Teams import ("migration") mode.

The whole feature hinges on three things:

  1. Create the team with `@microsoft.graph.teamCreationMode: migration` and an
     explicit backdated `createdDateTime`.
  2. Create channels the same way, then POST messages carrying their original
     `createdDateTime` and `from.user`.
  3. Call `completeMigration` on every channel, then on the team, then add
     members. Members cannot be added while the team is locked.

Ordering rule enforced everywhere below:
    team.created <= channel.created <= min(message.created)
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import msal
import requests

from .http import ApiError, HttpClient
from .state import State

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = ["https://graph.microsoft.com/.default"]
LARGE_FILE_THRESHOLD = 4 * 1024 * 1024


class GraphAuth:
    def __init__(self) -> None:
        self.app = msal.ConfidentialClientApplication(
            client_id=os.environ["MS_CLIENT_ID"],
            client_credential=os.environ["MS_CLIENT_SECRET"],
            authority=f"https://login.microsoftonline.com/{os.environ['MS_TENANT_ID']}",
        )
        self._token: str | None = None
        self._exp = 0.0
        self._lock = threading.Lock()

    def header(self) -> dict[str, str]:
        with self._lock:
            if not self._token or time.time() > self._exp - 120:
                res = self.app.acquire_token_for_client(scopes=SCOPE)
                if "access_token" not in res:
                    raise RuntimeError(f"Graph auth failed: {res}")
                self._token = res["access_token"]
                self._exp = time.time() + int(res.get("expires_in", 3600))
            return {"Authorization": f"Bearer {self._token}"}


class GraphClient:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.auth = GraphAuth()
        self.http = HttpClient(
            GRAPH,
            self.auth.header,
            rate_per_sec=cfg["graph"].get("rate_limit_rps", 4.0),
            max_retries=cfg["graph"].get("max_retries", 8),
        )

    # ---- users -----------------------------------------------------------

    def find_user(self, email: str) -> dict[str, Any] | None:
        esc = email.replace("'", "''")
        body = self.http.get(
            "/users",
            params={"$filter": f"mail eq '{esc}' or userPrincipalName eq '{esc}'",
                    "$select": "id,displayName,userPrincipalName,mail"},
        )
        vals = body.get("value") or []
        return vals[0] if vals else None

    # ---- team / channel creation (migration mode) -------------------------

    def create_team_migration(self, display_name: str, created_dt: str,
                              description: str = "",
                              owner_upn: str | None = None) -> str:
        body = {
            "@microsoft.graph.teamCreationMode": "migration",
            "template@odata.bind": f"{GRAPH}/teamsTemplates('standard')",
            "displayName": display_name,
            "description": description or f"Migrated from Zoho Cliq",
            "createdDateTime": created_dt,
        }
        resp = self.http.post("/teams", json=body)
        # 202 Accepted; the team id is in the Location header
        loc = resp.headers.get("Location", "")
        m = re.search(r"teams\('([^']+)'\)", loc) or re.search(r"/teams/([0-9a-fA-F-]{36})", loc)
        if not m:
            raise RuntimeError(f"Could not parse team id from Location: {loc!r}")
        team_id = m.group(1)
        self._await_team(team_id)
        return team_id

    def _await_team(self, team_id: str, timeout: int = 600) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.http.get(f"/teams/{team_id}")
                return
            except ApiError as e:
                if e.status not in (404, 409):
                    raise
            time.sleep(5)
        raise TimeoutError(f"team {team_id} did not provision within {timeout}s")

    def create_channel_migration(self, team_id: str, display_name: str,
                                 created_dt: str, description: str = "") -> str:
        resp = self.http.post(f"/teams/{team_id}/channels", json={
            "@microsoft.graph.channelCreationMode": "migration",
            "displayName": display_name[:50],
            "description": description[:1024],
            "createdDateTime": created_dt,
        })
        return resp.json()["id"]

    # ---- chats: 1:1 and group (chat migration mode) -----------------------
    #
    # Chats cannot be *created* in migration mode the way teams and channels
    # can. The sequence is: create (or resolve) the chat, then startMigration
    # with a backdated conversationCreationDateTime, then import, then
    # completeMigration. Only the app that called startMigration may import
    # into that thread until it completes the migration.
    #
    # The startMigration/completeMigration reference pages are published under
    # /beta with a v1.0 selector, so v1.0 is tried first and beta is the
    # fallback. Everything else is v1.0.

    def create_or_get_chat(self, member_ids: list[str], chat_type: str = "oneOnOne",
                           topic: str | None = None) -> str:
        """For oneOnOne, Graph returns the existing chat if the pair already has
        one — which is what we want: history lands in their real conversation."""
        # visibleHistoryStartDateTime is rejected on create ("cannot be set") —
        # it is only settable when adding a member to an existing chat. Members
        # added at creation predate the import, so they see the imported
        # history; a member added *later* would need remove-then-add with the
        # property set, which needs ChatMember.ReadWrite.All.
        members = [
            {
                "@odata.type": "#microsoft.graph.aadUserConversationMember",
                "roles": ["owner"],
                "user@odata.bind": f"{GRAPH}/users('{uid}')",
            }
            for uid in member_ids
        ]

        body: dict[str, Any] = {"chatType": chat_type, "members": members}
        if chat_type == "group" and topic:
            body["topic"] = topic[:250]
        return self.http.post("/chats", json=body).json()["id"]

    def get_chat(self, chat_id: str) -> dict[str, Any]:
        return self.http.get(f"/chats/{chat_id}")

    def _chat_migration_call(self, chat_id: str, action: str,
                             body: dict[str, Any] | None = None) -> None:
        paths = [f"/chats/{chat_id}/{action}",
                 f"https://graph.microsoft.com/beta/chats/{chat_id}/{action}"]
        last: ApiError | None = None
        for i, path in enumerate(paths):
            try:
                self.http.post(path, json=body)
                return
            except ApiError as e:
                last = e
                # v1.0 not serving the action yet -> try beta
                if i == 0 and e.status in (404, 405, 501):
                    log.info("%s not available on v1.0; retrying on beta", action)
                    continue
                # Only a genuine "already in that state" is idempotent. This
                # used to match any 400 containing "migration", which swallowed
                # real completeMigration failures and left chats stuck in
                # migration mode while state.db recorded them as completed —
                # the Teams client then shows "Migration for this conversation
                # is in progress" and refuses to render the full history.
                body = e.body.lower()
                already = ("already" in body
                           or "not in migration" in body
                           or "invalid state" in body)
                if e.status == 400 and already:
                    log.warning("chat %s: %s reports the state is already set "
                                "(%s)", chat_id, action, e.body[:200])
                    return
                raise
        if last:
            raise last

    def start_chat_migration(self, chat_id: str, created_dt: str) -> None:
        self._chat_migration_call(chat_id, "startMigration",
                                  {"conversationCreationDateTime": created_dt})

    def complete_chat_migration(self, chat_id: str) -> None:
        self._chat_migration_call(chat_id, "completeMigration")

    def import_chat_message(self, chat_id: str, payload: dict[str, Any]) -> str:
        return self.http.post(f"/chats/{chat_id}/messages", json=payload).json()["id"]

    # ---- group chat history visibility ------------------------------------
    #
    # Every member of a group chat carries a visibleHistoryStartDateTime: the
    # earliest message they are allowed to see. A member added when the chat is
    # created gets "now", so backdated imported messages fall before their
    # cutoff and stay hidden from them. The property cannot be set at creation
    # and cannot be patched afterwards, so the documented remedy is to remove
    # the member and add them back with the value backdated.
    # 1:1 chats have no such property, which is why only group chats are affected.

    def list_chat_members(self, chat_id: str) -> list[dict[str, Any]]:
        return self.http.get(f"/chats/{chat_id}/members").get("value", [])

    def remove_chat_member(self, chat_id: str, membership_id: str) -> None:
        self.http.request("DELETE", f"/chats/{chat_id}/members/{membership_id}")

    def add_chat_member(self, chat_id: str, aad_user_id: str,
                        visible_since: str, roles: list[str] | None = None) -> None:
        self.http.post(f"/chats/{chat_id}/members", json={
            "@odata.type": "#microsoft.graph.aadUserConversationMember",
            "user@odata.bind": f"{GRAPH}/users/{aad_user_id}",
            "visibleHistoryStartDateTime": visible_since,
            "roles": roles if roles is not None else ["owner"],
        })

    def count_chat_messages(self, chat_id: str) -> int:
        n = 0
        url = f"/chats/{chat_id}/messages?$top=50"
        while url:
            body = self.http.get(url)
            n += len(body.get("value", []))
            url = body.get("@odata.nextLink")
        return n

    # ---- files -----------------------------------------------------------

    def drive_web_url(self, drive_id: str) -> str:
        """Base for building attachment content URLs -- see item_path_url."""
        return (self.http.get(f"/drives/{drive_id}?$select=webUrl")
                .get("webUrl") or "").rstrip("/")

    def user_drive(self, user_id: str) -> tuple[str, str]:
        """(drive_id, drive_web_url). The web URL is the base for building
        attachment content URLs — see item_path_url."""
        d = self.http.get(f"/users/{user_id}/drive?$select=id,webUrl")
        return d["id"], (d.get("webUrl") or "").rstrip("/")

    def get_drive_item(self, drive_id: str, item_id: str) -> dict[str, Any]:
        return self.http.get(f"/drives/{drive_id}/items/{item_id}")

    @staticmethod
    def item_path_url(drive_web_url: str, item: dict[str, Any]) -> str:
        """A durable URL that ends in the real filename.

        Teams derives an attachment's file type from the last path segment of
        contentUrl and rejects the message with 403 "File attachment without
        extension is not supported in application context" when it can't. A
        driveItem's own webUrl is useless here for Office documents, where it
        takes the form `/_layouts/15/Doc.aspx?sourcedoc={guid}&...`. The
        path-addressed form always ends in the filename."""
        rel = (item.get("parentReference", {}).get("path") or "")
        rel = rel.split("root:", 1)[-1] if "root:" in rel else ""
        return drive_web_url + quote(f"{rel}/{item['name']}")

    @staticmethod
    def needs_extension(name: str | None) -> bool:
        return "." not in (name or "").rsplit("/", 1)[-1]

    def ensure_folder(self, drive_id: str, parts: list[str]) -> str:
        """Walk a folder path, creating what's missing. Returns the leaf id.
        Path-addressed uploads don't create intermediate folders, so this does."""
        parent = "root"
        for name in parts:
            safe = re.sub(r'[<>:"/\\|?*]', "_", name).strip(". ")[:200] or "folder"
            try:
                item = self.http.get(f"/drives/{drive_id}/items/{parent}:/{safe}")
                parent = item["id"]
                continue
            except ApiError as e:
                if e.status != 404:
                    raise
            item = self.http.post(
                f"/drives/{drive_id}/items/{parent}/children",
                json={"name": safe, "folder": {},
                      "@microsoft.graph.conflictBehavior": "replace"},
            ).json()
            parent = item["id"]
        return parent

    def create_org_link(self, drive_id: str, item_id: str) -> str | None:
        """A reference attachment is just a URL; the recipient still needs
        permission to open it. An organization-scoped view link grants it."""
        try:
            body = self.http.post(
                f"/drives/{drive_id}/items/{item_id}/createLink",
                json={"type": "view", "scope": "organization"},
            ).json()
            return (body.get("link") or {}).get("webUrl")
        except ApiError as e:
            log.warning("createLink failed for %s: %s", item_id, e)
            return None

    def channel_files_folder(self, team_id: str, channel_id: str) -> tuple[str, str]:
        """Returns (drive_id, folder_item_id). First call also provisions it."""
        for attempt in range(6):
            try:
                body = self.http.get(f"/teams/{team_id}/channels/{channel_id}/filesFolder")
                return body["parentReference"]["driveId"], body["id"]
            except ApiError as e:
                if e.status not in (404, 503):
                    raise
                time.sleep(5 * (attempt + 1))
        raise RuntimeError("filesFolder never provisioned")

    def upload_file(self, drive_id: str, folder_id: str, name: str,
                    local_path: Path) -> dict[str, Any]:
        size = local_path.stat().st_size
        safe = re.sub(r'[<>:"/\\|?*]', "_", name)[:250]

        if size <= LARGE_FILE_THRESHOLD:
            with open(local_path, "rb") as fh:
                resp = self.http.request(
                    "PUT",
                    f"/drives/{drive_id}/items/{folder_id}:/{safe}:/content",
                    data=fh.read(),
                    headers={"Content-Type": "application/octet-stream"},
                )
            return resp.json()

        # resumable upload session for >4 MB
        sess = self.http.post(
            f"/drives/{drive_id}/items/{folder_id}:/{safe}:/createUploadSession",
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        ).json()
        url = sess["uploadUrl"]
        chunk = 5 * 1024 * 1024  # must be a multiple of 320 KiB
        with open(local_path, "rb") as fh:
            pos = 0
            while pos < size:
                data = fh.read(chunk)
                end = pos + len(data) - 1
                r = requests.put(
                    url, data=data, timeout=300,
                    headers={"Content-Length": str(len(data)),
                             "Content-Range": f"bytes {pos}-{end}/{size}"},
                )
                if r.status_code in (200, 201):
                    return r.json()
                if r.status_code != 202:
                    raise ApiError(r.status_code, r.text, url)
                pos = end + 1
        raise RuntimeError("upload session ended without a completed item")

    @staticmethod
    def etag_guid(drive_item: dict[str, Any]) -> str:
        """Reference attachments must use the driveItem's eTag GUID as their id."""
        etag = drive_item.get("eTag", "")
        m = re.search(r"([0-9a-fA-F-]{36})", etag)
        return m.group(1) if m else drive_item["id"]

    # ---- message import ---------------------------------------------------

    def import_message(self, team_id: str, channel_id: str, payload: dict[str, Any],
                       reply_to: str | None = None) -> str:
        path = (f"/teams/{team_id}/channels/{channel_id}/messages"
                if not reply_to else
                f"/teams/{team_id}/channels/{channel_id}/messages/{reply_to}/replies")
        return self.http.post(path, json=payload).json()["id"]

    # ---- completion -------------------------------------------------------

    def list_channels(self, team_id: str) -> list[dict[str, Any]]:
        body = self.http.get(f"/teams/{team_id}/channels")
        return body.get("value", [])

    def create_plain_channel(self, team_id: str, display_name: str,
                            description: str = "") -> str:
        """A normal channel, not a migration-mode one.

        Migration-mode channel creation only works while the parent team is
        itself in migration state. Once completeMigration has run on the team
        that door is shut, so to backdate content into a new channel you create
        it normally and then call startMigration on it."""
        return self.http.post(f"/teams/{team_id}/channels", json={
            "displayName": display_name[:50],
            "description": description[:1024],
            "membershipType": "standard",
        }).json()["id"]

    def start_channel_migration(self, team_id: str, channel_id: str,
                               created_dt: str) -> None:
        """Reopen an existing channel for backdated import. Documented under
        /beta with a v1.0 selector, so try v1.0 first."""
        body = {"conversationCreationDateTime": created_dt}
        paths = [f"/teams/{team_id}/channels/{channel_id}/startMigration",
                 f"https://graph.microsoft.com/beta/teams/{team_id}"
                 f"/channels/{channel_id}/startMigration"]
        for i, path in enumerate(paths):
            try:
                self.http.post(path, json=body)
                return
            except ApiError as e:
                if i == 0 and e.status in (404, 405, 501):
                    log.info("channel startMigration not on v1.0; trying beta")
                    continue
                if e.status == 400 and "migration" in e.body.lower():
                    log.info("channel already in migration mode")
                    return
                raise

    def complete_channel(self, team_id: str, channel_id: str) -> None:
        self.http.post(f"/teams/{team_id}/channels/{channel_id}/completeMigration")

    def complete_team(self, team_id: str) -> None:
        self.http.post(f"/teams/{team_id}/completeMigration")

    def add_member(self, team_id: str, aad_user_id: str, owner: bool = False) -> None:
        self.http.post(f"/teams/{team_id}/members", json={
            "@odata.type": "#microsoft.graph.aadUserConversationMember",
            "roles": ["owner"] if owner else [],
            "user@odata.bind": f"{GRAPH}/users('{aad_user_id}')",
        })

    def count_channel_messages(self, team_id: str, channel_id: str) -> int:
        return self._message_stats(
            f"/teams/{team_id}/channels/{channel_id}/messages?$top=50")[0]

    def _message_stats(self, url: str) -> tuple[int, str | None, str | None]:
        """(count, oldest createdDateTime, newest createdDateTime).

        The dates are what prove a backdated import actually landed in the past
        rather than at import time — a count alone cannot show that.
        """
        n = 0
        oldest = newest = None
        while url:
            body = self.http.get(url)
            for m in body.get("value", []):
                n += 1
                dt = m.get("createdDateTime")
                if not dt:
                    continue
                if oldest is None or dt < oldest:
                    oldest = dt
                if newest is None or dt > newest:
                    newest = dt
            url = body.get("@odata.nextLink")
        return n, oldest, newest

    def channel_message_stats(self, team_id: str,
                              channel_id: str) -> tuple[int, str | None, str | None]:
        return self._message_stats(
            f"/teams/{team_id}/channels/{channel_id}/messages?$top=50")

    def chat_message_stats(self, chat_id: str) -> tuple[int, str | None, str | None]:
        return self._message_stats(f"/chats/{chat_id}/messages?$top=50")

    def list_chat_messages(self, chat_id: str) -> list[dict[str, Any]]:
        """Every message in a chat, tombstones excluded."""
        out: list[dict[str, Any]] = []
        url = f"/chats/{chat_id}/messages?$top=50"
        while url:
            body = self.http.get(url)
            out += [m for m in body.get("value", []) if not m.get("deletedDateTime")]
            url = body.get("@odata.nextLink")
        return out

    def soft_delete_chat_message(self, chat_id: str, message_id: str) -> None:
        """Needs Chat.ManageDeletion.All. Recoverable via undoSoftDelete."""
        self.http.post(f"/chats/{chat_id}/messages/{message_id}/softDelete")


# --------------------------------------------------------------------------
# Load phases
# --------------------------------------------------------------------------

def _tenant_directory(gc: GraphClient) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """One pass over the directory, indexed by email local-part and by
    normalized display name. Cheaper and more forgiving than a $filter per user.
    A user indexes under both mail and UPN, so dedupe by object id."""
    users: list[dict[str, Any]] = []
    url = ("/users?$select=id,displayName,mail,userPrincipalName,accountEnabled"
           "&$top=999")
    while url:
        body = gc.http.get(url)
        users += body.get("value", [])
        url = body.get("@odata.nextLink")

    by_local: dict[str, dict[str, dict]] = {}
    by_name: dict[str, dict[str, dict]] = {}
    for u in users:
        if u.get("accountEnabled") is False:
            continue
        for addr in (u.get("mail"), u.get("userPrincipalName")):
            if addr and "@" in addr:
                by_local.setdefault(addr.split("@")[0].lower(), {})[u["id"]] = u
        key = re.sub(r"[^a-z]", "", (u.get("displayName") or "").lower())
        if key:
            by_name.setdefault(key, {})[u["id"]] = u

    log.info("directory: %d enabled users", len(users))
    return ({k: list(v.values())[0] for k, v in by_local.items() if len(v) == 1},
            {k: list(v.values()) for k, v in by_name.items()})


def map_users(gc: GraphClient, st: State, cfg: dict[str, Any]) -> tuple[int, int]:
    """Resolve every Zoho user to an AAD user.

    Cliq and the tenant use different domains, so an exact email match alone
    leaves most of the directory unmapped. Resolution order:
      1. mapping/users.csv, when strategy=csv
      2. exact email
      3. email with the domain rewritten per mapping.domain_rewrite
      4. unique display-name match, if mapping.match_display_name
    Anything still unresolved is written to a CSV for manual correction —
    never guessed at."""
    import csv

    strategy = cfg["mapping"]["strategy"]
    rewrites: dict[str, str] = cfg["mapping"].get("domain_rewrite") or {}
    use_name = bool(cfg["mapping"].get("match_display_name"))

    manual: dict[str, str] = {}
    if strategy == "csv":
        with open("mapping/users.csv", newline="", encoding="utf-8") as fh:
            manual = {r["zoho_email"].lower(): r["aad_upn"] for r in csv.DictReader(fh)}

    by_local, by_name = _tenant_directory(gc)

    def resolve(u: Any) -> tuple[dict | None, str]:
        email = (u["zoho_email"] or "").strip().lower()
        if email and email in manual:
            hit = gc.find_user(manual[email])
            if hit:
                return hit, "csv"
        if email and "@" in email:
            local, domain = email.split("@", 1)
            hit = by_local.get(local)
            if hit:
                # same local part; the domain is only informational from here
                return hit, "email" if domain in (
                    (hit.get("mail") or "@").split("@")[-1].lower(),
                    (hit.get("userPrincipalName") or "@").split("@")[-1].lower(),
                ) else f"domain {domain}->{hit['userPrincipalName'].split('@')[-1]}"
            if domain in rewrites:
                hit = gc.find_user(f"{local}@{rewrites[domain]}")
                if hit:
                    return hit, f"rewrite {domain}->{rewrites[domain]}"
        if use_name:
            key = re.sub(r"[^a-z]", "", (u["display_name"] or "").lower())
            cands = by_name.get(key) or []
            if len(cands) == 1:
                return cands[0], "display name"
            if len(cands) > 1:
                return None, f"display name ambiguous ({len(cands)} matches)"
        return None, "not in tenant" if email else "no email in Cliq"

    mapped = unmapped = 0
    rows = st.rows("SELECT * FROM users WHERE status != 'mapped'")
    report = Path(cfg["paths"]["report_dir"]) / "users_unmapped.csv"
    report.parent.mkdir(parents=True, exist_ok=True)

    with open(report, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["zoho_id", "zoho_email", "display_name", "reason"])
        for u in rows:
            aad, how = resolve(u)
            if aad:
                st.set_user_mapping(u["zoho_id"], aad["id"],
                                    aad["userPrincipalName"], "mapped", how)
                mapped += 1
            else:
                w.writerow([u["zoho_id"], u["zoho_email"], u["display_name"], how])
                st.set_user_mapping(u["zoho_id"], None, None, "unmapped", how)
                unmapped += 1
    log.info("mapped=%d unmapped=%d (see %s)", mapped, unmapped, report)
    return mapped, unmapped


def load_messages(gc: GraphClient, st: State, cfg: dict[str, Any]) -> int:
    """Import messages channel by channel, strictly in timestamp order so that
    replies always find an already-imported parent."""
    from .transform import build_message_payload

    orphan_upn = cfg["mapping"].get("orphan_author_upn")
    orphan = gc.find_user(orphan_upn) if orphan_upn else None

    total = 0
    chats = st.rows(
        "SELECT * FROM chats WHERE teams_channel_id IS NOT NULL AND kind='channel'"
    )
    for chat in chats:
        team_id, chan_id = chat["teams_team_id"], chat["teams_channel_id"]
        drive_id = folder_id = None

        while True:
            batch = st.pending_messages(chat["zoho_chat_id"], limit=200)
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

                # Every attachment on this message, not just the ones already
                # downloaded — a file left out here would vanish from the
                # message with nothing recording the loss. There is no size cap:
                # upload_file switches to a resumable session above 4 MB.
                files = st.rows(
                    "SELECT * FROM files WHERE zoho_msg_id=? AND status!='skipped'",
                    (mid,),
                )
                if files and drive_id is None:
                    drive_id, folder_id = gc.channel_files_folder(team_id, chan_id)

                uploaded = []
                try:
                    for f in files:
                        if f["sp_etag_guid"]:
                            uploaded.append(dict(f))
                            continue
                        blob = Path(f["local_path"] or "")
                        if not blob.exists():
                            log.error("blob missing on disk for %s (%s) — "
                                      "re-run extract-files",
                                      f["name"], f["local_path"] or "never downloaded")
                            st.mark("files", "zoho_file_id", f["zoho_file_id"],
                                    "pending",
                                    error="blob missing on disk; re-run extract-files")
                            continue
                        item = gc.upload_file(
                            drive_id, folder_id, f["name"] or f["zoho_file_id"],
                            blob,
                        )
                        guid = gc.etag_guid(item)
                        st.mark("files", "zoho_file_id", f["zoho_file_id"], "done",
                                sp_item_id=item["id"], sp_etag_guid=guid,
                                sp_web_url=item.get("webUrl"))
                        uploaded.append({**dict(f), "sp_etag_guid": guid,
                                         "sp_web_url": item.get("webUrl")})

                    payload = build_message_payload(st, row, uploaded, orphan)
                    parent_teams_id = None
                    if row["parent_msg_id"]:
                        p = st.one("SELECT teams_msg_id FROM messages WHERE zoho_msg_id=?",
                                   (row["parent_msg_id"],))
                        parent_teams_id = p["teams_msg_id"] if p else None

                    new_id = gc.import_message(team_id, chan_id, payload, parent_teams_id)
                    st.mark("messages", "zoho_msg_id", mid, "done", teams_msg_id=new_id)
                    total += 1
                    progressed = True
                except ApiError as e:
                    st.mark("messages", "zoho_msg_id", mid, "pending", error=str(e))
                    log.warning("msg %s failed: %s", mid, e)

            if not progressed:
                log.error("channel %s: a batch of %d made no progress; stopping "
                          "to avoid an endless retry loop",
                          chat["title"], len(batch))
                break

        log.info("channel %s complete", chat["title"])
    return total


def complete_migration(gc: GraphClient, st: State) -> None:
    """Channels first (including auto-created General), then the team, then members."""
    for team in st.rows("SELECT * FROM teams_ WHERE migration_done=0 AND teams_team_id IS NOT NULL"):
        tid = team["teams_team_id"]
        for ch in st.rows(
            "SELECT * FROM chats WHERE teams_team_id=? AND teams_channel_id IS NOT NULL", (tid,)
        ):
            stuck = st.one(
                "SELECT COUNT(*) n FROM messages WHERE zoho_chat_id=? AND status NOT IN ('done','skipped')",
                (ch["zoho_chat_id"],),
            )
            if stuck and stuck["n"]:
                log.error("refusing to complete %s: %d messages not imported",
                          ch["title"], stuck["n"])
                return
            try:
                gc.complete_channel(tid, ch["teams_channel_id"])
            except ApiError as e:
                if e.status not in (400, 409):   # already completed
                    raise
        # Complete the auto-created General channel too
        for c in gc.list_channels(tid):
            if c.get("displayName", "").lower() == "general":
                try:
                    gc.complete_channel(tid, c["id"])
                except ApiError as e:
                    if e.status not in (400, 409):
                        raise
                break
        gc.complete_team(tid)
        st.mark("teams_", "team_key", team["team_key"], "done", migration_done=1)
        log.info("team %s migration completed", team["display_name"])

        for u in st.rows("SELECT * FROM users WHERE status='mapped' AND aad_id IS NOT NULL"):
            try:
                gc.add_member(tid, u["aad_id"])
            except ApiError as e:
                log.warning("add_member %s: %s", u["aad_upn"], e)
