"""CLI entrypoint. Every subcommand is independently re-runnable."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from . import transform, zoho  # graph imported lazily: msal not needed for extract
from .state import State


def setup(cfg_path: str) -> tuple[dict, State]:
    load_dotenv()
    cfg = yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8"))
    for key in ("raw_dir", "blob_dir", "report_dir"):
        Path(cfg["paths"][key]).mkdir(parents=True, exist_ok=True)
    st = State(cfg["paths"]["state_db"])
    return cfg, st


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(prog="c2t", description="Zoho Cliq -> Microsoft Teams migration")
    ap.add_argument("-c", "--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("init", "extract-users", "extract-chats",
              "extract-files", "map-users", "plan", "load-teams", "load-messages",
              "export-dms", "complete", "verify", "verify-extract",
              "verify-teams", "status",
              "plan-dms", "verify-dms", "status-dms"):
        sub.add_parser(c)
    em = sub.add_parser("extract-messages",
                        help="walk every chat back to its first message")
    em.add_argument("--rescan", action="store_true",
                    help="re-walk chats already marked extracted, from now back "
                         "to the true start — idempotent, picks up new messages "
                         "and re-proves nothing old is missing")
    em.add_argument("--only", default=None,
                    help="a single chat title or Zoho chat id (implies --rescan)")
    cd = sub.add_parser("complete-dms",
                        help="take chats out of migration mode — until this "
                             "succeeds Teams shows the 'migration in progress' "
                             "banner and hides older messages")
    cd.add_argument("--force", action="store_true",
                    help="re-issue completeMigration even for chats this "
                         "database already calls completed; a 204 means the "
                         "chat was in fact still open")
    dd = sub.add_parser("dedupe-chats",
                        help="soft-delete duplicate copies left by repeated "
                             "import runs (dry run unless --apply)")
    dd.add_argument("--only", default=None,
                    help="a single chat title or Zoho chat id")
    dd.add_argument("--apply", action="store_true",
                    help="actually delete; without this it only reports")
    ro = sub.add_parser("reopen-chats",
                        help="re-run startMigration on completed chats with a "
                             "far-older creation date so clients render the "
                             "whole history")
    ro.add_argument("--floor", default=None,
                    help="ISO8601 conversationCreationDateTime to set "
                         "(default 2024-01-01T00:00:00Z); must be strictly "
                         "older than every message in the archive")
    ro.add_argument("--only", default=None,
                    help="a single chat title or Zoho chat id — pilot with this")
    ro.add_argument("--dry-run", action="store_true",
                    help="show what would change without touching Teams")
    sh = sub.add_parser("share-history",
                        aliases=["share-group-history"],
                        help="make imported history visible: re-add chat members "
                             "with a backdated visibleHistoryStartDateTime")
    sh.add_argument("--kind", choices=["dm", "group", "all"], default="all",
                    help="which chats to fix (default: all)")
    sh.add_argument("--only", default=None,
                    help="a single chat title or Zoho chat id")
    sh.add_argument("--floor", default=None,
                    help="set every member's visibleHistoryStartDateTime to this "
                         "ISO8601 date instead of 48h before the oldest message "
                         "(e.g. 2024-01-01T00:00:00Z). Chats whose members sit "
                         "well before the first message are the ones that render "
                         "full history in the Teams client")
    ac = sub.add_parser("import-as-channel",
                        help="import one chat into a Teams channel instead of a "
                             "chat — for bot chats and departed users")
    ac.add_argument("chat", help="chat title or Zoho chat id")
    ac.add_argument("--name", default=None, help="channel name to create")
    bc = sub.add_parser("bundle-chat",
                        help="build a portable HTML folder with linked attachments")
    bc.add_argument("chat", help="chat title or Zoho chat id")
    ld = sub.add_parser("load-dms", help="import 1:1 (and optionally group) chats")
    ld.add_argument("--only", default=None,
                    help="a single chat title or Zoho chat id — use this to pilot")
    ld.add_argument("--limit-chats", type=int, default=None,
                    help="process at most N chats, smallest first")
    r = sub.add_parser("run", help="run a whole phase group unattended")
    r.add_argument("--phase", choices=["extract", "load", "all"], default="extract")
    r.add_argument("--yes", action="store_true",
                   help="proceed through the irreversible completeMigration gate")
    r.add_argument("--log-file", default=None)
    d = sub.add_parser("dump", help="write extracted data to disk")
    d.add_argument("--format", choices=["jsonl", "json"], default="jsonl")
    p = sub.add_parser("probe", help="dump one raw page from an endpoint")
    p.add_argument("endpoint", nargs="?", default="all",
                   help="channels | chats | users | all")

    args = ap.parse_args(argv)
    cfg, st = setup(args.config)
    cmd = args.cmd

    if cmd == "init":
        print(f"state initialised at {cfg['paths']['state_db']}")

    elif cmd == "run":
        from . import orchestrate
        if args.log_file:
            fh = logging.FileHandler(args.log_file, encoding="utf-8")
            fh.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
            logging.getLogger().addHandler(fh)
        try:
            if args.phase in ("extract", "all"):
                orchestrate.run_extract(cfg, st)
            if args.phase in ("load", "all"):
                orchestrate.run_load(cfg, st, assume_yes=args.yes)
        except orchestrate.GateFailure as e:
            print(f"\nGATE: {e}", file=sys.stderr)
            return 2
        print("\n" + json.dumps(st.counts(), indent=2))

    elif cmd == "dump":
        n = transform.dump(st, cfg, args.format)
        print(f"{n} messages written to {cfg['paths']['raw_dir']}")

    elif cmd == "probe":
        zc = zoho.ZohoClient(cfg)
        out = Path(cfg["paths"]["report_dir"])
        names = (["channels", "chats", "messages", "types", "users"]
                 if args.endpoint == "all" else [args.endpoint])
        for name in names:
            try:
                zoho.probe(zc, name, out)
            except Exception as e:
                import traceback
                print(f"\n=== {name}  ->  FAILED: {type(e).__name__}: {e}")
                traceback.print_exc()

    elif cmd.startswith("extract"):
        zc = zoho.ZohoClient(cfg)
        if cmd == "extract-users":
            print(f"{zoho.extract_users(zc, st)} users")
        elif cmd == "extract-chats":
            print(f"{zoho.extract_chats(zc, st)} chats")
        elif cmd == "extract-messages":
            n = zoho.extract_messages(zc, st, rescan=args.rescan, only=args.only)
            print(f"{n} messages")
        elif cmd == "extract-files":
            print(f"{zoho.extract_files(zc, st, Path(cfg['paths']['blob_dir']))} files")

    elif cmd == "map-users":
        from . import graph
        gc = graph.GraphClient(cfg)
        mapped, unmapped = graph.map_users(gc, st, cfg)
        print(f"mapped={mapped} unmapped={unmapped}")
        if unmapped:
            print(f"fix {cfg['paths']['report_dir']}/users_unmapped.csv, "
                  f"switch mapping.strategy to csv, and re-run", file=sys.stderr)

    elif cmd == "plan":
        plans = transform.plan(st, cfg)
        out = Path(cfg["paths"]["report_dir"]) / "plan.json"
        out.write_text(json.dumps(plans, indent=2), encoding="utf-8")
        for p in plans:
            total = sum(c["messages"] for c in p["channels"])
            print(f"\nTEAM  {p['team_key']}  (created {p['team_created']})")
            for c in p["channels"]:
                print(f"  ├─ {c['title'][:44]:<44} {c['messages']:>7} msgs")
            print(f"  └─ {len(p['channels'])} channels, {total} messages")
        bad = st.one("SELECT COUNT(*) n FROM users WHERE status='unmapped'")
        if bad and bad["n"]:
            print(f"\n!! {bad['n']} unmapped users — resolve before load", file=sys.stderr)
            return 1
        print(f"\nplan written to {out}")

    elif cmd == "load-teams":
        from . import graph
        gc = graph.GraphClient(cfg)
        transform.create_containers(gc, st, cfg)
        print("teams and channels created in migration mode")

    elif cmd == "load-messages":
        from . import graph
        gc = graph.GraphClient(cfg)
        print(f"{graph.load_messages(gc, st, cfg)} messages imported")

    elif cmd == "export-dms":
        print(f"{transform.export_dms(st, cfg)} DM/group transcripts exported")

    elif cmd == "plan-dms":
        from . import chats, graph
        plans = chats.plan_dms(st, cfg, graph.GraphClient(cfg))
        out = Path(cfg["paths"]["report_dir"]) / "plan_dms.json"
        out.write_text(json.dumps(plans, indent=2), encoding="utf-8")
        gb = sum(p["file_bytes"] for p in plans if not p["blocked"]) / 1e9
        msgs = sum(p["messages_importable"] for p in plans if not p["blocked"])
        print(f"{'chat':<26}{'type':<10}{'ppl':>4}{'msgs':>7}{'files':>7}  status")
        for p in plans:
            status = ("BLOCKED: " + p["blocked"]) if p["blocked"] else (
                "already created" if p["teams_chat_id"] else "will create")
            print(f"{(p['title'] or '')[:25]:<26}{p['chat_type']:<10}"
                  f"{p['participants']:>4}{p['messages_importable']:>7}"
                  f"{p['files']:>7}  {status}")
            for n in p["notes"]:
                print(f"{'':<26}  - {n}")
        ready = [p for p in plans if not p["blocked"]]
        print(f"\n{len(ready)}/{len(plans)} chats importable: "
              f"{msgs} messages, {gb:.2f} GB of attachments")
        print(f"plan written to {out}")
        if not ready:
            return 1

    elif cmd == "load-dms":
        from . import chats, graph
        n = chats.load_dms(graph.GraphClient(cfg), st, cfg,
                           only=args.only, limit_chats=args.limit_chats)
        print(f"{n} messages imported into Teams chats")
        print("chats remain in migration mode — run `complete-dms` when the "
              "counts look right")

    elif cmd == "status-dms":
        from . import chats
        chats.status_dms(st, cfg)

    elif cmd == "complete-dms":
        from . import chats, graph
        n = chats.complete_dms(graph.GraphClient(cfg), st, force=args.force)
        print(f"{n} chats completed")

    elif cmd == "bundle-chat":
        from . import chats
        print(f"bundle written to {chats.bundle_chat(st, cfg, args.chat)}")

    elif cmd == "import-as-channel":
        from . import chats, graph
        n = chats.import_chat_as_channel(graph.GraphClient(cfg), st, cfg,
                                         args.chat, args.name)
        print(f"{n} messages imported into the channel")

    elif cmd == "dedupe-chats":
        from . import chats, graph
        n = chats.dedupe_chats(graph.GraphClient(cfg), st, only=args.only,
                               dry_run=not args.apply)
        print(f"{n} duplicate copies {'found' if not args.apply else 'removed'}")

    elif cmd == "reopen-chats":
        from . import chats, graph
        n = chats.reopen_chats(graph.GraphClient(cfg), st, cfg,
                               floor=args.floor or chats.REOPEN_FLOOR,
                               only=args.only, dry_run=args.dry_run)
        print(f"{n} chats {'would be' if args.dry_run else ''} re-opened")

    elif cmd in ("share-history", "share-group-history"):
        from . import chats, graph
        n = chats.share_history(graph.GraphClient(cfg), st, cfg,
                                kind=args.kind, only=args.only, floor=args.floor)
        print(f"{n} chat memberships re-shared with full history")

    elif cmd == "verify-extract":
        zc = zoho.ZohoClient(cfg)
        short = zoho.verify_extract(zc, st)
        st.close()
        return 1 if short else 0

    elif cmd == "verify-teams":
        from . import chats, graph
        bad = chats.verify_teams(graph.GraphClient(cfg), st, cfg)
        st.close()
        return 1 if bad else 0

    elif cmd == "verify-dms":
        from . import chats, graph
        return 1 if chats.verify_dms(graph.GraphClient(cfg), st) else 0

    elif cmd == "complete":
        from . import graph
        gc = graph.GraphClient(cfg)
        graph.complete_migration(gc, st)
        print("migration completed; teams are now writable")

    elif cmd == "verify":
        from . import graph
        gc = graph.GraphClient(cfg)
        rows = st.rows("SELECT * FROM chats WHERE teams_channel_id IS NOT NULL")
        bad = 0
        print(f"{'channel':<40}{'source':>9}{'teams':>9}{'delta':>8}")
        for c in rows:
            src = st.one(
                "SELECT COUNT(*) n FROM messages WHERE zoho_chat_id=? AND status='done'",
                (c["zoho_chat_id"],))["n"]
            dst = gc.count_channel_messages(c["teams_team_id"], c["teams_channel_id"])
            delta = dst - src
            bad += delta != 0
            print(f"{(c['title'] or '')[:39]:<40}{src:>9}{dst:>9}{delta:>8}")
        print(f"\n{len(rows)} channels, {bad} with a non-zero delta")
        return 1 if bad else 0

    elif cmd == "status":
        print(json.dumps(st.counts(), indent=2))

    st.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())