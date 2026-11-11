"""Read-only data layer for the local dashboard: view models and queries."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from dropship import db
from dropship.config import Settings
from dropship.margins import load_supplier_quotes
from dropship.models import Candidate
from dropship.monitor import proposal_details

_FREE_QUOTA = {"canopy": 100}
# page/outcome name -> proposals.status
_ORDER_STATUS = {"pending": "pending", "approved": "approved_manual",
                 "approved_manual": "approved_manual", "rejected": "rejected"}
_OUTCOME = {"pending": "pending", "approved_manual": "approved", "rejected": "rejected"}


def amazon_url(asin: str) -> str:
    return f"https://www.amazon.com/dp/{asin}"


def open_readonly(db_path: Path) -> sqlite3.Connection | None:
    path = Path(db_path)
    if not path.exists():
        return None
    # as_uri() percent-encodes '#', '%' and spaces, so the ?mode=ro query always applies.
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass(frozen=True)
class RunSummary:
    id: int
    started_at: str
    finished_at: str | None
    status: str
    duration_s: float | None
    fetched: int
    dropped: int
    proposed: int


@dataclass(frozen=True)
class OrderView:
    id: int
    asin: str
    title: str
    qty: int
    unit_cost: float
    total: float
    risk_score: int
    cost_label: str
    narrative: str
    status: str
    created_at: str
    decided_at: str | None
    amazon_url: str


@dataclass(frozen=True)
class StageView:
    stage: str
    status: str
    attempts: int
    error_type: str
    message: str
    started_at: str
    finished_at: str


@dataclass(frozen=True)
class DashboardData:
    last_run: RunSummary | None
    pending_count: int
    approved_today: float
    daily_cap: float
    requests_this_month: int
    free_quota: int | None
    tokens_today: int
    pending: list[OrderView]
    last_errors: list[StageView]


@dataclass(frozen=True)
class RunDetail:
    summary: RunSummary
    stages: list[StageView]
    candidates: list[Candidate]
    dropped: list[dict]
    tokens: int


@dataclass(frozen=True)
class ProductRow:
    asin: str
    title: str
    price: float | None
    margin_pct: float | None
    verdict: str | None
    outcome: str
    last_run_id: int
    amazon_url: str


def _summary(conn: sqlite3.Connection, run: dict) -> RunSummary:
    rid = run["id"]
    fetched = conn.execute(
        "SELECT COUNT(*) FROM (SELECT asin FROM candidates WHERE run_id = ?1 AND asin != '-'"
        " UNION SELECT asin FROM dropped WHERE run_id = ?1 AND stage = 'research'"
        " AND asin != '-')", (rid,)).fetchone()[0]
    dropped = conn.execute(
        "SELECT COUNT(*) FROM dropped WHERE run_id = ? AND asin != '-'", (rid,)).fetchone()[0]
    proposed = conn.execute(
        "SELECT COUNT(*) FROM proposals WHERE run_id = ?", (rid,)).fetchone()[0]
    duration = None
    if run["finished_at"] is not None:
        duration = (datetime.fromisoformat(run["finished_at"])
                    - datetime.fromisoformat(run["started_at"])).total_seconds()
    return RunSummary(rid, run["started_at"], run["finished_at"], run["status"],
                      duration, fetched, dropped, proposed)


def _order_view(conn: sqlite3.Connection, p) -> OrderView:
    row = conn.execute(
        "SELECT created_at, decided_at FROM proposals WHERE id = ?", (p.id,)).fetchone()
    title, label = proposal_details(conn, p)
    return OrderView(p.id, p.asin, title, p.qty, p.unit_cost, p.total, p.risk_score, label,
                     p.narrative, p.status, row["created_at"], row["decided_at"],
                     amazon_url(p.asin))


def dashboard(conn: sqlite3.Connection, s: Settings, today: date) -> DashboardData:
    run_id = db.latest_run_id(conn)
    run = db.get_run(conn, run_id) if run_id is not None else None
    pending = orders(conn, "pending")
    errors = []
    if run_id is not None:
        errors = [StageView(**st) for st in db.get_stage_runs(conn, run_id) if st["error_type"]]
    return DashboardData(
        last_run=_summary(conn, run) if run else None,
        pending_count=len(pending),
        approved_today=db.approved_total_today(conn),
        daily_cap=s.daily_spend_cap,
        requests_this_month=db.source_requests_in_month(
            conn, s.data_source, today.year, today.month),
        free_quota=_FREE_QUOTA.get(s.data_source),
        tokens_today=db.llm_tokens_today(conn),
        pending=pending,
        last_errors=errors,
    )


def orders(conn: sqlite3.Connection, status: str) -> list[OrderView]:
    if status not in _ORDER_STATUS:
        raise ValueError(f"unknown order status: {status!r}")
    props = db.list_proposals(conn, _ORDER_STATUS[status])
    return [_order_view(conn, p) for p in reversed(props)]


def runs(conn: sqlite3.Connection) -> list[RunSummary]:
    rows = conn.execute(
        "SELECT id, started_at, finished_at, status FROM runs ORDER BY id DESC").fetchall()
    return [_summary(conn, dict(r)) for r in rows]


def run_detail(conn: sqlite3.Connection, run_id: int, reason: str | None) -> RunDetail | None:
    run = db.get_run(conn, run_id)
    if run is None:
        return None
    dropped = db.get_dropped(conn, run_id)
    if reason:
        dropped = [d for d in dropped if reason.lower() in d["reason"].lower()]
    tokens = conn.execute(
        "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM llm_usage WHERE run_id = ?",
        (run_id,)).fetchone()[0]
    return RunDetail(
        summary=_summary(conn, run),
        stages=[StageView(**st) for st in db.get_stage_runs(conn, run_id)],
        candidates=db.get_candidates(conn, run_id),
        dropped=dropped,
        tokens=int(tokens),
    )


def products(conn: sqlite3.Connection, q: str | None) -> list[ProductRow]:
    """One row per ASIN seen in any run, newest activity first."""
    info: dict[str, dict] = {}  # asin -> {"title", "cand": row|None, "runs": {run: {...}}}

    def entry(asin: str, run_id: int) -> dict:
        e = info.setdefault(asin, {"title": "?", "cand": None, "runs": {}})
        return e["runs"].setdefault(run_id, {"proposal": None, "drop": None, "cand": False})

    for r in conn.execute(
        "SELECT run_id, asin, title, sell_price, margin_pct, llm_verdict FROM candidates"
        " ORDER BY run_id, id"
    ):
        entry(r["asin"], r["run_id"])["cand"] = True
        info[r["asin"]].update(title=r["title"], cand=r)
    for r in conn.execute(
        "SELECT run_id, asin, title, reason FROM dropped WHERE asin != '-' ORDER BY run_id, id"
    ):
        entry(r["asin"], r["run_id"])["drop"] = r["reason"]
        if info[r["asin"]]["cand"] is None:
            info[r["asin"]]["title"] = r["title"]
    for r in conn.execute("SELECT run_id, asin, status FROM proposals ORDER BY id"):
        entry(r["asin"], r["run_id"])["proposal"] = r["status"]

    needle = q.lower() if q else None
    rows = []
    for asin, e in info.items():
        if needle and needle not in e["title"].lower() and needle not in asin.lower():
            continue
        last = max(e["runs"])
        seen = e["runs"][last]
        if seen["proposal"] is not None:
            outcome = _OUTCOME[seen["proposal"]]
        elif seen["drop"] is not None:
            outcome = f"dropped: {seen['drop']}"
        else:
            outcome = "candidate"
        c = e["cand"]
        rows.append(ProductRow(
            asin, e["title"], c["sell_price"] if c else None,
            c["margin_pct"] if c else None, c["llm_verdict"] if c else None,
            outcome, last, amazon_url(asin)))
    rows.sort(key=lambda r: (-r.last_run_id, r.asin))
    return rows


def settings_lines(s: Settings) -> list[str]:
    return [
        f"Data source: {s.data_source} ({', '.join(map(str, s.categories))})",
        f"Products must cost ${s.price_min:g}–{s.price_max:g}",
        f"Minimum rating {s.min_rating:g}, at least {s.min_reviews} reviews",
        f"Minimum margin {s.min_margin_pct:.0%} (fee {s.fee_pct:.0%}, shipping"
        f" ${s.shipping_cost:g}, estimated cost {s.est_cost_ratio:.0%} of price)",
        f"Max ${s.max_order_value:g} per order, ${s.daily_spend_cap:g} per day,"
        f" {s.max_units_per_order} units",
        f"Risk score must be at most {s.max_risk_score}",
        f"Re-proposal cooldown: {s.proposal_cooldown_days} days",
        f"Blocked brands: {', '.join(s.blocked_brands) or 'none'}",
    ]


def quotes(s: Settings) -> list[tuple[str, float]]:
    return sorted(load_supplier_quotes(s.supplier_quotes_path).items())
