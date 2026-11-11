from __future__ import annotations

import json
import sqlite3
from typing import Protocol

from pydantic import BaseModel

from dropship.config import Settings
from dropship.db import (
    approved_total_today,
    get_candidates,
    get_proposal,
    has_pending_proposal,
    insert_proposal,
    record_dropped,
    recently_decided,
    set_proposal_status,
)
from dropship.llm import LLM, LLMBudgetExceeded, LLMUnusable
from dropship.models import Candidate, Product, Proposal, StageResult
from dropship.risk import risk_score

RISK_SYSTEM = (
    "You are a cautious purchasing risk analyst for a small dropshipping business. "
    "You are given one candidate product with its deterministic risk score and the "
    "individual risk factors behind it. Write a brief narrative (2-3 sentences) "
    "explaining the main risks and whether a trial purchase looks reasonable, and "
    "suggest an integer trial quantity to buy (start small; prefer fewer units when "
    "risk is higher or the cost is only estimated). A human will review your "
    "suggestion before anything is bought."
)

FALLBACK_NARRATIVE = "LLM unavailable - deterministic risk only"


class RiskNote(BaseModel):
    narrative: str
    suggested_qty: int


class ApprovalError(Exception):
    """A proposal could not be approved or rejected."""


class Purchaser(Protocol):
    def place(self, p: Proposal) -> str: ...


class RecordOnlyPurchaser:
    def place(self, p: Proposal) -> str:
        return f"manual-{p.id}"


def _fit_qty(qty: int, unit_cost: float, s: Settings) -> int:
    qty = max(1, min(qty, s.max_units_per_order))
    while qty >= 1 and qty * unit_cost > s.max_order_value:
        qty -= 1
    return qty  # 0 means even one unit does not fit


def _best_candidates(cands: list[Candidate], s: Settings) -> list[Candidate]:
    """The candidates worth risk-scoring: the LLM's "promising" ones or, when nothing
    was reviewed (research degraded / no LLM), the top `llm_top_n` by score."""
    if any(c.llm_verdict != "unreviewed" for c in cands):
        return [c for c in cands if c.llm_verdict == "promising"]
    return sorted(cands, key=lambda c: c.score, reverse=True)[: s.llm_top_n]


def run_risk(
    llm: LLM | None,
    conn: sqlite3.Connection,
    run_id: int,
    products: dict[str, Product],
    s: Settings,
) -> StageResult:
    degraded_error = ""
    all_cands = get_candidates(conn, run_id)
    best = _best_candidates(all_cands, s)
    best_ids = {id(c) for c in best}
    drops: list[tuple[str, str, str, str]] = [
        (c.asin, c.title, "risk", f"verdict {c.llm_verdict}")
        for c in all_cands
        if id(c) not in best_ids
    ]
    for c in best:
        product = products.get(c.asin)
        if product is None or has_pending_proposal(conn, c.asin):
            continue
        if recently_decided(conn, c.asin, s.proposal_cooldown_days):
            drops.append((c.asin, c.title, "risk",
                          f"cooldown: decided within {s.proposal_cooldown_days} days"))
            continue
        ra = risk_score(c, product, s)
        if ra.risk_score > s.max_risk_score:
            drops.append((c.asin, c.title, "risk",
                          f"risk {ra.risk_score} > max {s.max_risk_score}"))
            continue

        narrative = FALLBACK_NARRATIVE
        suggested = 1
        try:
            if llm is None:
                raise LLMUnusable("no LLM available")
            user = json.dumps(
                {
                    "asin": c.asin,
                    "title": c.title,
                    "sell_price": c.sell_price,
                    "supplier_cost": c.supplier_cost,
                    "cost_source": c.cost_source,
                    "margin_pct": c.margin_pct,
                    "risk_score": ra.risk_score,
                    "factors": ra.factors,
                },
                separators=(",", ":"),
            )
            note = llm.parse(
                model="claude-opus-5-5",
                system=RISK_SYSTEM,
                user=user,
                schema=RiskNote,
                effort="medium",
                fallbacks=True,
            )
            narrative, suggested = note.narrative, note.suggested_qty
        except (LLMBudgetExceeded, LLMUnusable) as e:
            degraded_error = degraded_error or type(e).__name__

        qty = _fit_qty(suggested, c.supplier_cost, s)
        if qty < 1:
            continue
        insert_proposal(
            conn,
            Proposal(
                id=None,
                run_id=run_id,
                asin=c.asin,
                qty=qty,
                unit_cost=c.supplier_cost,
                total=qty * c.supplier_cost,
                risk_score=ra.risk_score,
                narrative=narrative,
                status="pending",
            ),
        )
    record_dropped(conn, run_id, drops)
    if degraded_error:
        return StageResult(
            stage="risk", status="degraded", attempts=1, error_type=degraded_error,
            message="LLM unavailable for some proposals; deterministic risk only",
        )
    return StageResult(stage="risk", status="ok", attempts=1)


def approve(conn: sqlite3.Connection, pid: int, s: Settings, purchaser: Purchaser) -> str:
    p = get_proposal(conn, pid)
    if p is None:
        raise ApprovalError(f"proposal {pid} not found")
    if p.status != "pending":
        raise ApprovalError(f"proposal {pid} is {p.status}, not pending")
    if p.total > s.max_order_value:
        raise ApprovalError(
            f"order total {p.total} exceeds max order value {s.max_order_value}"
        )
    today = approved_total_today(conn)
    if today + p.total > s.daily_spend_cap:
        raise ApprovalError(
            f"approving would exceed daily spend cap ({today} + {p.total} > {s.daily_spend_cap})"
        )
    ref = purchaser.place(p)
    set_proposal_status(conn, pid, "approved_manual")
    return ref


def reject(conn: sqlite3.Connection, pid: int) -> None:
    p = get_proposal(conn, pid)
    if p is None:
        raise ApprovalError(f"proposal {pid} not found")
    if p.status != "pending":
        raise ApprovalError(f"proposal {pid} is {p.status}, not pending")
    set_proposal_status(conn, pid, "rejected")
