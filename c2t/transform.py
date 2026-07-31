"""Transform: Cliq message shapes -> Teams import payloads.

Teams accepts a restricted HTML subset in message bodies. Everything is escaped
first and only known-safe markup is reintroduced.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .state import State

# Cliq mention forms seen in the wild. Extend as you find more.
MENTION_PATTERNS = [
    re.compile(r"\{@(?P<id>[^}|]+)(?:\|(?P<name>[^}]+))?\}"),
    re.compile(r"@\[(?P<name>[^\]]+)\]\((?P<id>[^)]+)\)"),
]

CODE_BLOCK = re.compile(r"```(?:\w+)?\n(.*?)```", re.DOTALL)
INLINE_CODE = re.compile(r"`([^`\n]+)`")
BOLD = re.compile(r"\*([^*\n]+)\*")
ITALIC = re.compile(r"_([^_\n]+)_")
STRIKE = re.compile(r"~([^~\n]+)~")
URL = re.compile(r"(https?://[^\s<>\"]+)")


def iso(ms: int) -> str:
    """Epoch milliseconds -> the ISO8601 form Graph expects."""
    return (datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z")


def backdate(ms: int, hours: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc) - timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


# --------------------------------------------------------------------------
# Body rendering
# --------------------------------------------------------------------------

def render_body(text: str, mentions: list[dict[str, Any]]) -> str:
    """Escape, then reintroduce the markup Teams tolerates. Mentions are emitted
    as <at id="N"> and their index must line up with the payload's mentions[]."""
    if not text:
        return ""

    # protect code blocks from markdown processing
    blocks: list[str] = []

    def stash(m: re.Match[str]) -> str:
        blocks.append(m.group(1))
        return f"\x00CODE{len(blocks) - 1}\x00"

    text = CODE_BLOCK.sub(stash, text)
    out = html.escape(text)

    for pat in MENTION_PATTERNS:
        def repl(m: re.Match[str]) -> str:
            gd = m.groupdict()
            idx = len(mentions)
            mentions.append({"_zoho_id": gd.get("id"),
                             "_name": gd.get("name") or gd.get("id")})
            return f'<at id="{idx}">{html.escape(gd.get("name") or "")}</at>'
        out = pat.sub(repl, out)

    out = INLINE_CODE.sub(r"<code>\1</code>", out)
    out = BOLD.sub(r"<strong>\1</strong>", out)
    out = ITALIC.sub(r"<em>\1</em>", out)
    out = STRIKE.sub(r"<s>\1</s>", out)
    out = URL.sub(r'<a href="\1">\1</a>', out)
    out = out.replace("\n", "<br>")

    for i, b in enumerate(blocks):
        out = out.replace(f"\x00CODE{i}\x00", f"<pre><code>{html.escape(b)}</code></pre>")
    return out


def build_message_payload(st: State, row: Any, uploaded_files: list[dict[str, Any]],
                          orphan: dict[str, Any] | None,
                          ts_override: int | None = None) -> dict[str, Any]:
    """Assemble one importable chatMessage.

    Identical for channels and chats. `ts_override` exists because
    createdDateTime must be unique to the millisecond within a thread; a 409
    is retried with the timestamp nudged forward."""
    payload_src = json.loads(row["payload"])

    author = st.one(
        "SELECT * FROM users WHERE zoho_id=? AND status='mapped'", (row["author_zoho_id"],)
    )
    if author:
        from_user = {"id": author["aad_id"],
                     "displayName": author["display_name"],
                     "userIdentityType": "aadUser"}
    elif orphan:
        from_user = {"id": orphan["id"],
                     "displayName": payload_src.get("author_name") or "Former Cliq user",
                     "userIdentityType": "aadUser"}
    else:
        raise ValueError(f"unmapped author {row['author_zoho_id']} and no orphan fallback")

    raw_mentions: list[dict[str, Any]] = []
    body = render_body(payload_src.get("text", ""), raw_mentions)

    # Resolve mention placeholders to AAD identities and rewrite each <at>
    # element in place. Two rules Graph enforces:
    #   - every entry in mentions[] must have a matching <at id="N"> in the body
    #   - the marker cannot be empty, and Cliq's `{@userid}` form carries no
    #     display name, so the name has to come from the resolved user
    # Unresolvable mentions lose the marker entirely, leaving plain text.
    mentions = []
    for i, m in enumerate(raw_mentions):
        u = st.one("SELECT * FROM users WHERE zoho_id=? AND status='mapped'",
                   (m["_zoho_id"],))
        tag = re.compile(rf'<at id="{i}">.*?</at>', re.DOTALL)
        if not u:
            fallback = html.escape(m["_name"] or "someone")
            body = tag.sub(fallback, body)
            continue
        shown = u["display_name"] or m["_name"] or u["aad_upn"] or "user"
        body = tag.sub(f'<at id="{i}">{html.escape(shown)}</at>', body)
        mentions.append({
            "id": i,
            "mentionText": shown,
            "mentioned": {"user": {"id": u["aad_id"],
                                   "displayName": u["display_name"],
                                   "userIdentityType": "aadUser"}},
        })

    # a declared mention whose marker somehow didn't survive would 400 the whole
    # message; drop it rather than lose the message
    mentions = [x for x in mentions if f'<at id="{x["id"]}">' in body]

    attachments = []
    for f in uploaded_files:
        guid = f.get("sp_etag_guid")
        if not guid:
            continue
        attachments.append({
            "id": guid,
            "contentType": "reference",
            "contentUrl": f.get("sp_web_url"),
            "name": f.get("name"),
        })
        body += f'<attachment id="{guid}"></attachment>'

    if not body.strip():
        body = "<i>(empty message)</i>"

    msg: dict[str, Any] = {
        "createdDateTime": iso(ts_override if ts_override is not None else row["ts"]),
        "from": {"user": from_user},
        "body": {"contentType": "html", "content": body},
    }
    if mentions:
        msg["mentions"] = mentions
    if attachments:
        msg["attachments"] = attachments
    return msg


# --------------------------------------------------------------------------
# Planning: decide teams, channel names and backdated container timestamps
# --------------------------------------------------------------------------

def plan(st: State, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    grouping = cfg["teams"]["grouping"]
    delim = cfg["teams"]["prefix_delimiter"]
    headroom = cfg["teams"]["backdate_headroom_hours"]
    single = cfg["teams"]["single_team_name"]

    rows = st.rows(
        "SELECT * FROM chats WHERE kind='channel' AND msg_count_src > 0 ORDER BY title"
    )
    groups: dict[str, list[Any]] = {}
    for c in rows:
        title = c["title"] or c["zoho_chat_id"]
        key = single if grouping == "one_team" else (
            title.split(delim, 1)[0].strip() if delim in title else "General"
        )
        groups.setdefault(key, []).append(c)

    plans = []
    with st.tx():
        for key, chans in groups.items():
            earliest = min(c["first_msg_ts"] for c in chans if c["first_msg_ts"])
            team_created = backdate(earliest, headroom * 2)
            st.db.execute(
                """INSERT INTO teams_(team_key, display_name, created_dt)
                   VALUES (?,?,?)
                   ON CONFLICT(team_key) DO UPDATE SET
                     display_name=excluded.display_name, created_dt=excluded.created_dt""",
                (key, key, team_created),
            )
            for c in chans:
                st.db.execute("UPDATE chats SET team_key=? WHERE zoho_chat_id=?",
                              (key, c["zoho_chat_id"]))
            plans.append({
                "team_key": key,
                "team_created": team_created,
                "channels": [
                    {"title": c["title"],
                     "messages": c["msg_count_src"],
                     "channel_created": backdate(c["first_msg_ts"], headroom)}
                    for c in chans
                ],
            })
    return plans


def create_containers(gc: Any, st: State, cfg: dict[str, Any]) -> None:
    """Create migration-mode teams and channels for everything in the plan."""
    headroom = cfg["teams"]["backdate_headroom_hours"]

    owner_upn = cfg["graph"].get("default_owner_upn")
    for team in st.rows("SELECT * FROM teams_ WHERE teams_team_id IS NULL"):
        tid = gc.create_team_migration(team["display_name"], team["created_dt"],
                                       owner_upn=owner_upn)
        st.mark("teams_", "team_key", team["team_key"], "created", teams_team_id=tid)

    for team in st.rows("SELECT * FROM teams_ WHERE teams_team_id IS NOT NULL"):
        tid = team["teams_team_id"]
        for c in st.rows(
            "SELECT * FROM chats WHERE team_key=? AND teams_channel_id IS NULL",
            (team["team_key"],),
        ):
            created = backdate(c["first_msg_ts"], headroom)
            # never let a channel predate its team
            if created < team["created_dt"]:
                created = team["created_dt"]
            safe_name = re.sub(r'[#%&*{}/\\:<>?+|\"`]', '', (c["title"] or c["zoho_chat_id"]))[:50].rstrip()
            if not safe_name:
                safe_name = f"channel_{c['zoho_chat_id'][:8]}"
            cid = gc.create_channel_migration(tid, safe_name, created)
            st.mark("chats", "zoho_chat_id", c["zoho_chat_id"], "container_ready",
                    teams_team_id=tid, teams_channel_id=cid)


def dump(st: State, cfg: dict[str, Any], fmt: str = "jsonl") -> int:
    """Write everything extracted to disk with IDs intact. This artifact is the
    real deliverable of the extract phase — it replays into the loader without
    touching Zoho again."""
    out = Path(cfg["paths"]["raw_dir"])
    out.mkdir(parents=True, exist_ok=True)
    n = 0

    for chat in st.rows("SELECT * FROM chats WHERE msg_count_src > 0"):
        cid = chat["zoho_chat_id"]
        safe = re.sub(r"[^\w\-]", "_", chat["title"] or cid)[:80]
        records = []
        for m in st.rows("SELECT * FROM messages WHERE zoho_chat_id=? ORDER BY ts", (cid,)):
            rec = json.loads(m["payload"])
            rec["chat_id"] = cid
            rec["files"] = [
                {"id": f["zoho_file_id"], "name": f["name"], "size": f["size"],
                 "sha256": f["sha256"], "local_path": f["local_path"]}
                for f in st.rows("SELECT * FROM files WHERE zoho_msg_id=?", (m["zoho_msg_id"],))
            ]
            records.append(rec)

        if fmt == "jsonl":
            with open(out / f"{safe}_{cid}.jsonl", "w", encoding="utf-8") as fh:
                for r in records:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        else:
            (out / f"{safe}_{cid}.json").write_text(
                json.dumps({"chat": dict(chat), "messages": records},
                           ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
        n += len(records)

    (out / "manifest.json").write_text(json.dumps({
        "chats": [dict(c) for c in st.rows("SELECT * FROM chats")],
        "users": [dict(u) for u in st.rows("SELECT * FROM users")],
        "file_count": st.one("SELECT COUNT(*) n FROM files WHERE status='done'")["n"],
        "message_count": n,
    }, indent=2, default=str), encoding="utf-8")
    return n


# --------------------------------------------------------------------------
# DM fallback: self-contained HTML export
# --------------------------------------------------------------------------

DM_HTML = """<!doctype html><meta charset="utf-8">
<title>{title}</title>
<style>
 body{{font:14px/1.55 -apple-system,Segoe UI,sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem}}
 h1{{font-size:1.25rem;border-bottom:1px solid #ddd;padding-bottom:.5rem}}
 .m{{margin:.9rem 0}} .h{{color:#666;font-size:.8rem}} .a{{font-weight:600;color:#222}}
 pre{{background:#f6f6f6;padding:.6rem;overflow:auto}}
 .f{{color:#0a58ca}}
</style>
<h1>{title}</h1>
<p class=h>Exported from Zoho Cliq · {count} messages</p>
{body}
"""


def export_dms(st: State, cfg: dict[str, Any]) -> int:
    out_dir = Path(cfg["dms"]["export_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for chat in st.rows("SELECT * FROM chats WHERE kind IN ('dm','group')"):
        msgs = st.rows(
            "SELECT * FROM messages WHERE zoho_chat_id=? ORDER BY ts", (chat["zoho_chat_id"],)
        )
        if not msgs:
            continue
        parts = []
        for m in msgs:
            p = json.loads(m["payload"])
            when = datetime.fromtimestamp(m["ts"] / 1000, tz=timezone.utc)
            files = "".join(
                f'<div class=f>📎 {html.escape(a["name"])}</div>' for a in p.get("attachments", [])
            )
            parts.append(
                f'<div class=m><span class=a>{html.escape(p.get("author_name") or "?")}</span> '
                f'<span class=h>{when:%Y-%m-%d %H:%M UTC}</span><br>'
                f'{render_body(p.get("text", ""), [])}{files}</div>'
            )
        title = chat["title"] or chat["zoho_chat_id"]
        safe = re.sub(r"[^\w\- ]", "_", title)[:100]
        (out_dir / f"{safe}_{chat['zoho_chat_id']}.html").write_text(
            DM_HTML.format(title=html.escape(title), count=len(msgs), body="".join(parts)),
            encoding="utf-8",
        )
        n += 1
    return n