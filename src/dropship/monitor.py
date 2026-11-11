from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

import anthropic
import httpx

from dropship.buyer import run_risk
from dropship.config import Settings
from dropship.db import (
    finish_run,
    get_candidate,
    get_candidates,
    get_dropped,
    get_run,
    get_stage_runs,
    last_success,
    list_proposals,
    llm_tokens_today,
    record_stage,
    start_run,
)
from dropship.errors import DataSourceError
from dropship.keepa_client import KeepaOutOfTokens
from dropship.llm import LLM
from dropship.models import Product, Proposal, StageResult
from dropship.research import run_research
from dropship.sources import ProductSource

log = logging.getLogger(__name__)

TRANSIENT = (
    httpx.TimeoutException,
    httpx.NetworkError,  # ConnectError, ReadError, WriteError, CloseError
    httpx.RemoteProtocolError,
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
)

_MAX_REFILL_WAIT_S = 900
_STALE_AFTER = timedelta(hours=26)
_SEVERITY = {"ok": 0, "degraded": 1, "failed": 2}


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, TRANSIENT):
        return True
    return (
        isinstance(exc, DataSourceError)
        and not isinstance(exc, KeepaOutOfTokens)
        and exc.status_code is not None
        and (exc.status_code == 429 or exc.status_code >= 500)
    )


def _failed(name: str, attempts: int, exc: Exception) -> StageResult:
    return StageResult(
        stage=name,
        status="failed",
        attempts=attempts,
        error_type=type(exc).__name__,
        message=str(exc)[:500],
    )


def run_stage(
    name: str,
    fn: Callable[[], StageResult],
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = 3,
) -> StageResult:
    calls = 0
    transient_attempts = 0
    waited_for_tokens = False
    while True:
        calls += 1
        try:
            result = fn()
            return result.model_copy(update={"attempts": calls})
        except KeepaOutOfTokens as exc:
            if waited_for_tokens:
                return _failed(name, calls, exc)
            waited_for_tokens = True
            sleep(min(exc.refill_in_ms / 1000, _MAX_REFILL_WAIT_S))
        except Exception as exc:  # noqa: BLE001 - the playbook decides per type
            if not _is_transient(exc):
                return _failed(name, calls, exc)
            transient_attempts += 1
            if transient_attempts >= max_attempts:
                return _failed(name, calls, exc)
            sleep(2**transient_attempts)


def run_pipeline(
    conn: sqlite3.Connection,
    s: Settings,
    keepa: ProductSource,
    llm_factory: Callable[[int], LLM | None],
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    run_id = start_run(conn)
    llm = llm_factory(run_id)
    products: dict[str, Product] = {}

    def research() -> StageResult:
        nonlocal products
        result, products = run_research(keepa, llm, conn, run_id, s)
        return result

    def risk() -> StageResult:
        return run_risk(llm, conn, run_id, products, s)

    started = datetime.now().isoformat()
    research_result = run_stage("research", research, sleep)
    record_stage(conn, run_id, research_result, started, datetime.now().isoformat())

    started = datetime.now().isoformat()
    if research_result.status == "failed":
        risk_result = StageResult(stage="risk", status="skipped", attempts=0)
    else:
        risk_result = run_stage("risk", risk, sleep)
    record_stage(conn, run_id, risk_result, started, datetime.now().isoformat())

    status = max(
        (r.status for r in (research_result, risk_result) if r.status != "skipped"),
        key=lambda st: _SEVERITY[st],
        default="ok",
    )
    finish_run(conn, run_id, status)
    path = write_report(conn, run_id, s)
    maybe_alert(
        status, path, os.environ.get("ALERT_WEBHOOK_URL"), stages=get_stage_runs(conn, run_id)
    )
    return run_id


_COST_LABELS = {"quoted": "quoted", "estimated": "estimated cost"}


def proposal_details(conn: sqlite3.Connection, p: Proposal) -> tuple[str, str]:
    """Title and cost-source label for a proposal, from its run's candidate row."""
    c = get_candidate(conn, p.run_id, p.asin)
    if c is None:
        return "?", "unknown"
    return c.title, _COST_LABELS.get(c.cost_source, c.cost_source)


def write_report(conn: sqlite3.Connection, run_id: int, s: Settings) -> Path:
    run = get_run(conn, run_id)
    stages = get_stage_runs(conn, run_id)
    status = run["status"] if run else "unknown"
    lines = [f"# Dropship run {run_id}", ""]

    last = last_success(conn, "research")
    if last is None or datetime.now() - last > _STALE_AFTER:
        when = "never" if last is None else last.isoformat(timespec="minutes")
        lines += [
            f"**STALE:** no successful research stage in the last 26 hours (last success: {when}).",
            "",
        ]

    lines += ["## Summary", ""]
    lines.append(f"- Run: {run_id}")
    lines.append(f"- Status: {status}")
    if run:
        lines.append(f"- Started: {run['started_at']}")
        lines.append(f"- Finished: {run['finished_at'] or '-'}")
    lines.append("")

    lines += [
        "## Stages",
        "",
        "| Stage | Status | Attempts | Started | Finished |",
        "|---|---|---|---|---|",
    ]
    for st in stages:
        lines.append(
            f"| {st['stage']} | {st['status']} | {st['attempts']} |"
            f" {st['started_at']} | {st['finished_at']} |"
        )
    lines.append("")

    lines += ["## Top candidates", ""]
    cands = sorted(get_candidates(conn, run_id), key=lambda c: c.score, reverse=True)[:20]
    if cands:
        lines += [
            "| ASIN | Title | Price | Cost | Margin % | Score | Verdict |",
            "|---|---|---|---|---|---|---|",
        ]
        for c in cands:
            title = c.title[:60].replace("|", "/")
            lines.append(
                f"| {c.asin} | {title} | {c.sell_price:.2f} | {c.supplier_cost:.2f} |"
                f" {c.margin_pct * 100:.1f} | {c.score:.2f} | {c.llm_verdict} |"
            )
    else:
        lines.append("None.")
    lines.append("")

    lines += ["## Proposals awaiting approval", ""]
    pending = list_proposals(conn, "pending")
    if pending:
        for p in pending:
            title, cost = proposal_details(conn, p)
            narrative = " ".join(p.narrative.split())
            lines.append(
                f"- #{p.id} {p.asin} x{p.qty} total {p.total:.2f} risk {p.risk_score}"
                f" - `dropship approve {p.id}`"
            )
            lines.append(f"  - {title} - unit {p.unit_cost:.2f} ({cost})")
            lines.append(f"  - {narrative}")
    else:
        lines.append("None.")
    lines.append("")

    lines += ["## Dropped products", ""]
    dropped = get_dropped(conn, run_id)
    if dropped:
        lines += ["| ASIN | Title | Stage | Reason |", "|---|---|---|---|"]
        for d in dropped:
            title = d["title"][:60].replace("|", "/")
            reason = d["reason"].replace("|", "/")
            lines.append(f"| {d['asin']} | {title} | {d['stage']} | {reason} |")
    else:
        lines.append("None.")
    lines.append("")

    lines += ["## Errors", ""]
    errors = [st for st in stages if st["error_type"] or st["message"]]
    if errors:
        for st in errors:
            lines.append(f"- {st['stage']} ({st['status']}): {st['error_type']}: {st['message']}")
    else:
        lines.append("None.")
    lines.append("")

    lines += ["## LLM tokens today", "", str(llm_tokens_today(conn)), ""]

    s.reports_dir.mkdir(parents=True, exist_ok=True)
    path = s.reports_dir / f"{date.today().isoformat()}-run-{run_id}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


_ALERT_STAGE_KEYS = ("stage", "status", "error_type", "message")


def maybe_alert(
    run_status: str,
    report_path: Path,
    webhook_url: str | None,
    http: httpx.Client | None = None,
    stages: list[dict] | None = None,
) -> bool:
    if run_status not in ("failed", "degraded") or not webhook_url:
        return False
    summary = Path(report_path).read_text(encoding="utf-8")[:1500]
    payload = {
        "status": run_status,
        "report": str(report_path),
        "summary": summary,
        "stages": [{k: st[k] for k in _ALERT_STAGE_KEYS} for st in stages or []],
    }
    try:
        if http is None:
            with httpx.Client(timeout=10) as client:
                resp = client.post(webhook_url, json=payload)
        else:
            resp = http.post(webhook_url, json=payload)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:  # UnsupportedProtocol is an HTTPError
        log.warning("alert webhook failed: %s", type(exc).__name__)
        return False
    if not resp.is_success:
        log.warning("alert webhook returned HTTP %s", resp.status_code)
        return False
    return True
