#!/usr/bin/env python3
"""Patch 2: exhaustive message retrieval.

Fixes the truncation bug — Cliq's message endpoint returns no pagination token,
so anything past the first page was being silently dropped.

Applies:
  zoho.py   time-window (totime) pagination replacing token pagination
  zoho.py   edit metadata read from content.* instead of a nonexistent root key
  zoho.py   `probe types` to discover attachment message shapes
  zoho.py   per-chat completeness check against Zoho's total_message_count
  state.py  msg_count_reported column (additive; existing state.db is safe)
  cli.py    register the `types` probe

Idempotent. Backs up each file and reverts it if the result won't parse.
"""
from __future__ import annotations

import ast
import shutil
import sys
from pathlib import Path

NEW_MESSAGES = '''    def messages(self, chat_id: str, from_ts: int | None = None) -> Iterator[dict[str, Any]]:
        """Exhaustive message retrieval.

        The messages endpoint returns bare {"data": [...]} with no next_token
        and no has_more, so token pagination cannot work here. Cliq windows by
        time instead: we walk backwards, repeatedly asking for everything older
        than the oldest message seen so far, until a page comes back empty.

        Dedupes by message id because window boundaries can overlap, and stops
        on no-progress so a misbehaving endpoint cannot spin forever.
        """
        path = EP["chat_messages"].format(chat_id=chat_id)
        limit = self.page_size
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
                return                      # short page = start of history'''

PROBE_TYPES = '''    # `types` sweeps real history to find one example of every message shape.
    # Attachment handling cannot be written correctly without this.
    if name == "types":
        import json as _json

        chans = ZohoClient.extract_items(cli.http.get(EP["channels"], params={"limit": 50}))
        dms = ZohoClient.extract_items(cli.http.get(EP["chats"], params={"limit": 50}))
        targets = [(c.get("name"), c["chat_id"]) for c in chans + dms if c.get("chat_id")]

        samples: dict[str, dict[str, Any]] = {}
        scanned = 0
        for label, cid in targets:
            try:
                for m in cli.messages(cid):
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
        print(f"\\n=== types  ->  scanned {scanned} messages across {len(targets)} chats")
        for t, ex in sorted(samples.items()):
            ck = sorted((ex.get("content") or {}).keys())
            print(f"  type={t:<14} content keys: {ck}")
        print(f"\\nraw written to : {dest}")
        if not any(k in ("file", "attachment", "image", "audio", "video")
                   for k in samples):
            print("\\n!! no attachment-bearing message found — share a file in Cliq\\n"
                  "   and re-run, or file extraction stays unverified.")
        return samples

    path = EP[name].format(**fmt)'''

EDITS: dict[str, list[tuple[str, str, str]]] = {
    "c2t/zoho.py": [
        ("time-window message pagination",
         '''    def messages(self, chat_id: str, from_ts: int | None = None) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {}
        if from_ts:
            params["fromtime"] = from_ts
        yield from self.paginate(
            EP["chat_messages"].format(chat_id=chat_id), params, data_key="data"
        )''',
         NEW_MESSAGES),

        ("edit metadata from content.*",
         '        "edited": bool(raw.get("is_edited")),',
         '''        "edited": bool(content.get("edited")),
        "edited_time": as_int(content.get("edited_time")) or None,
        "revision": as_int(raw.get("revision")),
        "pinned": bool(raw.get("is_pinned")),'''),

        ("probe types (attachment discovery)",
         '    path = EP[name].format(**fmt)',
         PROBE_TYPES),

        ("store Zoho's reported message count",
         '''            # total_message_count is Zoho's own count -> free verification
            if ch.get("total_message_count") is not None:
                st.db.execute(
                    "UPDATE chats SET note=? WHERE zoho_chat_id=?",
                    (f"src_total={ch['total_message_count']}", str(ch["chat_id"])),
                )''',
         '''            # Zoho's own count -> lets extraction verify it got everything
            st.db.execute(
                "UPDATE chats SET msg_count_reported=? WHERE zoho_chat_id=?",
                (as_int(ch.get("total_message_count")), str(ch["chat_id"])),
            )'''),

        ("per-chat completeness check",
         '''        total += count
        log.info("chat %s (%s): +%d messages", cid, chat["title"], count)
    return total''',
         '''        total += count
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
    return total'''),
    ],
    "c2t/state.py": [
        ("msg_count_reported column migration",
         "        self.db.executescript(SCHEMA)",
         '''        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Additive-only column migrations. Safe to run against an existing db."""
        for table, col, decl in [
            ("chats", "msg_count_reported", "INTEGER"),   # Zoho's own count
        ]:
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")'''),
    ],
    "c2t/cli.py": [
        ("register the types probe",
         'names = (["channels", "chats", "messages", "users"]',
         'names = (["channels", "chats", "messages", "types", "users"]'),
    ],
}


def main() -> int:
    if not Path("c2t").is_dir():
        print("!! run this from the cliq2teams/ directory", file=sys.stderr)
        return 1

    failed = False
    for path_s, edits in EDITS.items():
        path = Path(path_s)
        if not path.exists():
            print(f"!! {path} missing"); failed = True; continue

        src = original = path.read_text(encoding="utf-8")
        applied, skipped = [], []
        for desc, old, new in edits:
            # Must test the FULL replacement: several anchors survive inside
            # their own replacement text, so a prefix check re-applies them.
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
        backup = path.with_suffix(path.suffix + ".bak2")
        shutil.copy2(path, backup)
        path.write_text(src, encoding="utf-8")
        try:
            ast.parse(src)
        except SyntaxError as e:
            shutil.copy2(backup, path)
            print(f"  !! invalid syntax ({e}); reverted from {backup}", file=sys.stderr)
            failed = True

    print("\nDONE" if not failed else "\nCOMPLETED WITH PROBLEMS — see ANCHOR NOT FOUND above")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())