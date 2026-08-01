"""Phase orchestration: run the pipeline unattended, with gates where a human
decision is genuinely required.

Design rules:
  - Every phase is idempotent, so the orchestrator can retry freely.
  - A phase that leaves failed rows triggers a bounded retry sweep before the
    run advances; transient throttling resolves itself, real errors don't.
  - Gates hard-stop on conditions that would produce a silently wrong result.
    `--yes` bypasses confirmation prompts but NEVER bypasses a gate.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable

from .state import State

log = logging.getLogger(__name__)


class GateFailure(RuntimeError):
    """A precondition that must be resolved by a human before continuing."""


# --------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------

def gate_extraction_complete(st: State, cfg: dict) -> None:
    stuck = st.one("SELECT COUNT(*) n FROM chats WHERE status='failed'")["n"]
    if stuck:
        raise GateFailure(
            f"{stuck} chats failed extraction. Inspect: "
            f"SELECT title, note FROM chats WHERE status='failed';"
        )
    empty = st.one("SELECT COUNT(*) n FROM chats WHERE msg_count_src=0")["n"]
    total = st.one("SELECT COUNT(*) n FROM chats")["n"]
    if total and empty == total:
        raise GateFailure("every chat extracted zero messages — endpoints are wrong")


def gate_users_mapped(st: State, cfg: dict) -> None:
    bad = st.one("SELECT COUNT(*) n FROM users WHERE status='unmapped'")["n"]
    if not bad:
        return
    if cfg["mapping"].get("orphan_author_upn"):
        log.warning("%d unmapped users will be attributed to the orphan account", bad)
        return
    raise GateFailure(
        f"{bad} unmapped users and no mapping.orphan_author_upn set. "
        f"Fill in {cfg['paths']['report_dir']}/users_unmapped.csv, set "
        f"mapping.strategy=csv, and re-run map-users."
    )


def gate_all_messages_loaded(st: State, cfg: dict) -> None:
    """The last line of defence before an irreversible completeMigration."""
    rows = st.rows(
        """SELECT c.title, COUNT(*) n FROM messages m
           JOIN chats c ON c.zoho_chat_id = m.zoho_chat_id
           WHERE m.status NOT IN ('done','skipped') AND c.teams_channel_id IS NOT NULL
           GROUP BY c.title"""
    )
    if rows:
        detail = ", ".join(f"{r['title']}={r['n']}" for r in rows[:8])
        raise GateFailure(
            f"refusing to complete migration: unimported messages remain ({detail}). "
            f"completeMigration is irreversible — the only fix afterwards is "
            f"deleting the team and starting over."
        )


# --------------------------------------------------------------------------
# Retry sweeps
# --------------------------------------------------------------------------

def sweep(st: State, table: str, pk: str, runner: Callable[[], Any],
          max_rounds: int = 3, backoff: int = 30) -> int:
    """Re-run a phase while it keeps making progress on failed rows."""
    for rnd in range(max_rounds):
        remaining = st.one(
            f"SELECT COUNT(*) n FROM {table} WHERE status NOT IN ('done','skipped')"
        )["n"]
        if not remaining:
            return 0
        log.info("sweep %s: %d rows outstanding (round %d/%d)",
                 table, remaining, rnd + 1, max_rounds)
        runner()
        after = st.one(
            f"SELECT COUNT(*) n FROM {table} WHERE status NOT IN ('done','skipped')"
        )["n"]
        if after == 0:
            return 0
        if after == remaining:
            log.warning("sweep %s made no progress; stopping at %d", table, after)
            return after
        time.sleep(backoff)
    return st.one(
        f"SELECT COUNT(*) n FROM {table} WHERE status NOT IN ('done','skipped')"
    )["n"]


# --------------------------------------------------------------------------
# Phase groups
# --------------------------------------------------------------------------

def run_extract(cfg: dict, st: State) -> None:
    """Fully unattended. Needs only Zoho credentials."""
    from . import transform, zoho

    zc = zoho.ZohoClient(cfg)

    log.info("[1/6] chats")
    zoho.extract_chats(zc, st)

    log.info("[2/6] messages")
    zoho.extract_messages(zc, st)
    sweep(st, "chats", "zoho_chat_id", lambda: zoho.extract_messages(zc, st))

    log.info("[3/6] users")
    zoho.extract_users(zc, st)          # falls back to message senders on 403

    log.info("[4/6] files")
    zoho.extract_files(zc, st, Path(cfg["paths"]["blob_dir"]))
    left = sweep(st, "files", "zoho_file_id",
                 lambda: zoho.extract_files(zc, st, Path(cfg["paths"]["blob_dir"])))
    if left:
        log.warning("%d files could not be downloaded; messages will import "
                    "without them", left)

    gate_extraction_complete(st, cfg)

    # Prove it rather than assume it: re-walk every chat to its first message and
    # compare with what was stored. This is the guarantee that the archive is
    # complete, so a shortfall stops the run before anything is loaded.
    log.info("[4b/6] verifying every chat reaches its first message")
    short = zoho.verify_extract(zc, st)
    if short:
        raise GateFailure(
            f"{short} chats are missing messages the API still returns. "
            f"Run `extract-messages --rescan`, then `verify-extract`."
        )

    log.info("[5/6] dump to disk")
    transform.dump(st, cfg)

    if cfg["dms"]["strategy"] == "html_export":
        log.info("[6/6] DM transcripts")
        transform.export_dms(st, cfg)

    log.info("extract complete: %s", st.counts())


def run_load(cfg: dict, st: State, assume_yes: bool = False) -> None:
    """Needs Graph admin consent. Gated before the irreversible step."""
    from . import graph, transform

    gc = graph.GraphClient(cfg)

    log.info("[1/5] map users")
    graph.map_users(gc, st, cfg)
    gate_users_mapped(st, cfg)

    log.info("[2/5] plan")
    transform.plan(st, cfg)

    log.info("[3/5] create teams and channels (migration mode)")
    transform.create_containers(gc, st, cfg)

    log.info("[4/5] import messages")
    graph.load_messages(gc, st, cfg)
    left = sweep(st, "messages", "zoho_msg_id",
                 lambda: graph.load_messages(gc, st, cfg), max_rounds=4, backoff=60)
    if left:
        raise GateFailure(f"{left} messages failed to import; not completing migration")

    gate_all_messages_loaded(st, cfg)

    if not assume_yes:
        raise GateFailure(
            "ready to complete migration. This is IRREVERSIBLE — teams can never "
            "be backdated again. Re-run with --yes to proceed."
        )

    log.info("[5/5] complete migration")
    graph.complete_migration(gc, st)
    log.info("load complete: %s", st.counts())