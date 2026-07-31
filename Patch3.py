#!/usr/bin/env python3
"""Patch 3: null chat_id routing + message type handling.

Fixes a silent data-loss bug found by `probe types`: channels the user has
never opened report chat_id=null, so their message URL became /chats/null/
messages and the entire channel was skipped.

Applies:
  zoho.py    address null-chat_id channels by unique_name via the channel
             endpoint, with a runtime fallback if the chat endpoint 400s
  zoho.py    mark `deleted` and `info` messages non-importable (stored for
             count reconciliation, never posted to Teams)
  zoho.py    defensive file-field extraction (id/file_id/fileId variants)
  graph.py   skip non-importable messages during load

Idempotent. Backs up each file and reverts it if the result won't parse.
"""
from __future__ import annotations

import ast
import shutil
import sys
from pathlib import Path

EDITS = {'c2t/zoho.py': [['route around null chat_id', '    def messages(self, chat_id: str, from_ts: int | None = None) -> Iterator[dict[str, Any]]:', '    def _message_path(self, chat_id: Any, unique_name: str | None) -> str | None:\n        """Channels the user has never opened report chat_id=null and must be\n        addressed by unique_name through the channel endpoint instead."""\n        cid = str(chat_id or "").strip()\n        if cid and cid.lower() not in ("none", "null", ""):\n            return EP["chat_messages"].format(chat_id=cid)\n        if unique_name:\n            return EP["channel_messages"].format(unique_name=unique_name)\n        return None\n\n    def messages(self, chat_id: str, from_ts: int | None = None,\n                 unique_name: str | None = None) -> Iterator[dict[str, Any]]:'], ['select message path with fallback', '        path = EP["chat_messages"].format(chat_id=chat_id)\n        limit = self.page_size', '        path = self._message_path(chat_id, unique_name)\n        if path is None:\n            log.error("chat %s: no usable chat_id or unique_name; SKIPPED", chat_id)\n            return\n        limit = self.page_size\n        alt_tried = False'], ['runtime fallback to channel endpoint', '            except ApiError as e:\n                if e.status == 400 and "extra_param_found" in e.body and totime is not None:', '            except ApiError as e:\n                if (e.status == 400 and not alt_tried and unique_name\n                        and "chats/" in path):\n                    alt_tried = True\n                    path = EP["channel_messages"].format(unique_name=unique_name)\n                    log.info("chat %s: falling back to channel endpoint (%s)",\n                             chat_id, unique_name)\n                    continue\n                if e.status == 400 and "extra_param_found" in e.body and totime is not None:'], ['mark deleted/info as non-importable', '    return {\n        "id": str(raw.get("id") or raw.get("time")),\n        "type": mtype,', '    # deleted -> tombstone, info -> system notice. Both are stored so counts\n    # reconcile against Zoho\'s total, but neither is importable as a message.\n    importable = mtype not in ("deleted", "info")\n    if mtype == "deleted":\n        text = "[message deleted in Zoho Cliq]"\n    elif mtype == "info" and not text:\n        text = "[system notice]"\n\n    return {\n        "id": str(raw.get("id") or raw.get("time")),\n        "type": mtype,\n        "importable": importable,'], ['defensive file field extraction', '    f = content.get("file")\n    if isinstance(f, dict) and f.get("id"):\n        attachments.append({\n            "id": f["id"],\n            "name": f.get("name") or f.get("file_name") or f["id"],\n            "size": f.get("size"),\n        })', '    f = content.get("file")\n    if isinstance(f, dict):\n        fid = f.get("id") or f.get("file_id") or f.get("fileId")\n        if fid:\n            attachments.append({\n                "id": str(fid),\n                "name": f.get("name") or f.get("file_name") or str(fid),\n                "size": as_int(f.get("size")),\n                "content_type": f.get("content_type") or f.get("type"),\n            })'], ['probe types: carry unique_name', '        targets = [(c.get("name"), c["chat_id"]) for c in chans + dms if c.get("chat_id")]', '        targets = [(c.get("name"), c.get("chat_id"), c.get("unique_name"))\n                   for c in chans + dms]'], ['probe types: pass unique_name', '        for label, cid in targets:\n            try:\n                for m in cli.messages(cid):', '        for label, cid, uname in targets:\n            try:\n                for m in cli.messages(cid, unique_name=uname):'], ['extract_messages: pass unique_name', '                for raw in cli.messages(cid, from_ts):', '                for raw in cli.messages(cid, from_ts, chat["unique_name"]):'], ['stable key for null-chat_id channels', '            st.upsert_chat(\n                str(ch["chat_id"]),          # chat_id, not channel_id: messages\n                "channel",                   # are addressed by chat_id\n                name,', '            st.upsert_chat(\n                str(ch.get("chat_id") or ch.get("channel_id") or ch.get("unique_name")),\n                "channel",\n                name,'], ['reported count uses same key', '                (as_int(ch.get("total_message_count")), str(ch["chat_id"])),', '                (as_int(ch.get("total_message_count")),\n                 str(ch.get("chat_id") or ch.get("channel_id") or ch.get("unique_name"))),']], 'c2t/graph.py': [['skip non-importable messages at load', '                if row["attempts"] >= 5:', '                if not json.loads(row["payload"]).get("importable", True):\n                    st.mark("messages", "zoho_msg_id", mid, "skipped")\n                    continue\n\n                if row["attempts"] >= 5:'], ['import json in graph.py', 'import logging\nimport os', 'import json\nimport logging\nimport os']]}


def main() -> int:
    if not Path("c2t").is_dir():
        print("!! run this from the cliq2teams/ directory", file=sys.stderr)
        return 1

    failed = False
    for path_s, edit_list in EDITS.items():
        path = Path(path_s)
        if not path.exists():
            print(f"!! {path} missing"); failed = True; continue

        src = original = path.read_text(encoding="utf-8")
        applied, skipped = [], []
        for desc, old, new in edit_list:
            if new in src:
                skipped.append(f"{desc} (already applied)")
            elif old in src:
                src = src.replace(old, new, 1)
                applied.append(desc)
            else:
                skipped.append(f"{desc} (ANCHOR NOT FOUND)")
                failed = True

        print(f"\n{path}")
        for a in applied:
            print(f"  [applied] {a}")
        for s in skipped:
            print(f"  [skipped] {s}")

        if src == original:
            continue
        backup = path.with_suffix(path.suffix + ".bak3")
        shutil.copy2(path, backup)
        path.write_text(src, encoding="utf-8")
        try:
            ast.parse(src)
        except SyntaxError as e:
            shutil.copy2(backup, path)
            print(f"  !! invalid syntax ({e}); reverted from {backup}", file=sys.stderr)
            failed = True

    print("\nDONE" if not failed else "\nCOMPLETED WITH PROBLEMS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())