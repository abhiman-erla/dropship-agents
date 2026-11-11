from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

import anthropic

from dropship import db
from dropship.buyer import ApprovalError, RecordOnlyPurchaser, approve, reject
from dropship.config import ConfigError, Settings, load_config
from dropship.keepa_client import KeepaClient
from dropship.llm import LLM
from dropship.monitor import proposal_details, run_pipeline, write_report


def resolve_paths(s: Settings, config_path: Path) -> Settings:
    """Resolve relative paths against the config file's directory (cwd-independent)."""
    base = Path(config_path).resolve().parent
    return s.model_copy(update={
        "db_path": base / s.db_path,
        "reports_dir": base / s.reports_dir,
        "supplier_quotes_path": base / s.supplier_quotes_path,
    })


def make_llm_factory(conn: sqlite3.Connection, s: Settings) -> Callable[[int], LLM | None]:
    def factory(run_id: int) -> LLM | None:
        try:
            client = anthropic.Anthropic()
        except anthropic.AnthropicError:
            return None
        # The SDK does not raise without credentials; it fails later at request time.
        if not (client.api_key or client.auth_token or getattr(client, "credentials", None)):
            return None
        return LLM(client, conn, run_id, s.daily_llm_token_budget)

    return factory


def _cmd_run(conn: sqlite3.Connection, s: Settings, args: argparse.Namespace) -> int:
    keepa = KeepaClient(os.environ["KEEPA_API_KEY"], s.keepa_domain)
    run_id = run_pipeline(conn, s, keepa, make_llm_factory(conn, s))
    run = db.get_run(conn, run_id)
    status = run["status"] if run else "unknown"
    print(f"Run {run_id}: {status}")
    print(f"Report: {write_report(conn, run_id, s)}")
    return 1 if status == "failed" else 0


def _cmd_proposals(conn: sqlite3.Connection, s: Settings, args: argparse.Namespace) -> int:
    proposals = db.list_proposals(conn, args.status)
    if not proposals:
        print("No proposals.")
    for p in proposals:
        print(f"#{p.id}  {p.asin}  qty={p.qty}  total={p.total:.2f}  "
              f"risk={p.risk_score}  {p.status}")
        title, cost = proposal_details(conn, p)
        print(f"    {title}  unit={p.unit_cost:.2f} ({cost})")
        print(f"    {' '.join(p.narrative.split())}")
    return 0


def _cmd_approve(conn: sqlite3.Connection, s: Settings, args: argparse.Namespace) -> int:
    try:
        ref = approve(conn, args.id, s, RecordOnlyPurchaser())
    except ApprovalError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Approved #{args.id} — reference {ref}. Place this order with your supplier.")
    return 0


def _cmd_reject(conn: sqlite3.Connection, s: Settings, args: argparse.Namespace) -> int:
    try:
        reject(conn, args.id)
    except ApprovalError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Rejected #{args.id}.")
    return 0


def _cmd_report(conn: sqlite3.Connection, s: Settings, args: argparse.Namespace) -> int:
    run_id = args.run if args.run is not None else db.latest_run_id(conn)
    if run_id is None:
        print("No runs yet.")
        return 1
    print(write_report(conn, run_id, s))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dropship")
    parser.add_argument("--config", type=Path, default=Path("config.toml"),
                        help="path to config.toml (default: config.toml)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the research + risk pipeline")
    p = sub.add_parser("proposals", help="list proposals")
    p.add_argument("--status", choices=["pending", "approved_manual", "rejected"])
    p = sub.add_parser("approve", help="approve a pending proposal (records it only)")
    p.add_argument("id", type=int)
    p = sub.add_parser("reject", help="reject a pending proposal")
    p.add_argument("id", type=int)
    p = sub.add_parser("report", help="(re)write a run's report and print its path")
    p.add_argument("--run", type=int, help="run id (default: latest)")
    return parser


_COMMANDS = {
    "run": _cmd_run, "proposals": _cmd_proposals, "approve": _cmd_approve,
    "reject": _cmd_reject, "report": _cmd_report,
}


def main(argv: list[str] | None = None) -> int:
    # The Keepa key travels in the query string; httpx logs request URLs at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = _build_parser().parse_args(argv)
    try:
        s = resolve_paths(load_config(args.config), args.config)
    except (ConfigError, OSError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if args.command == "run" and not os.environ.get("KEEPA_API_KEY"):
        print("KEEPA_API_KEY is not set; export it before running `dropship run`.",
              file=sys.stderr)
        return 2
    conn = db.connect(s.db_path)
    try:
        return _COMMANDS[args.command](conn, s, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
