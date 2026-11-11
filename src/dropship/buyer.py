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
    list_proposals,
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
    "suggestion before anything is bought. "
    "Factors that are null and listed in unknown_factors mean the data was unavailable, "
    "not that the risk is low; treat them as uncertainty."
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


def _blocked(conn: sqlite3.Connection, asin: str, s: Settings, own: set[str]) -> bool:
    """Pending (from an earlier run) or recently decided: not eligible for a top-N slot.
    `own` holds ASINs this run already proposed, so a retried stage keeps its top N."""
    if asin in own:
        return False
    return has_pending_proposal(conn, asin) or recently_decided(
        conn, asin, s.proposal_cooldown_days
    )


def _top_n(
    conn: sqlite3.Connection, cands: list[Candidate], s: Settings, own: set[str]
) -> list[Candidate]:
    """The top `llm_top_n` by score among eligible candidates (as research sends them
    to the LLM)."""
    eligible = [c for c in cands if not _blocked(conn, c.asin, s, own)]
    return sorted(eligible, key=lambda c: c.score, reverse=True)[: s.llm_top_n]


def _best_candidates(
    conn: sqlite3.Connection, cands: list[Candidate], s: Settings, own: set[str]
) -> list[Candidate]:
    """The candidates worth risk-scoring: the LLM's "promising" ones or, when nothing
    was reviewed (research degraded / no LLM), the top `llm_top_n` eligible by score."""
    if any(c.llm_verdict != "unreviewed" for c in cands):
        return [c for c in cands if c.llm_verdict == "promising"]
    return _top_n(conn, cands, s, own)


def _drop_reason(
    conn: sqlite3.Connection, c: Candidate, top_ids: set[int], s: Settings
) -> str | None:
    """Why a candidate was not risk-scored (None: no drop row, e.g. already pending)."""
    if c.llm_verdict != "unreviewed":
        return f"verdict {c.llm_verdict}"
    if has_pending_proposal(conn, c.asin):
        return None  # it is listed under proposals awaiting approval
    if recently_decided(conn, c.asin, s.proposal_cooldown_days):
        return f"cooldown: decided within {s.proposal_cooldown_days} days"
    if id(c) in top_ids:
        return "verdict unreviewed"  # sent to the LLM, which returned no verdict for it
    return f"not in top {s.llm_top_n} by score"


def _with_unknown(narrative: str, unknown: list[str]) -> str:
    if not unknown:
        return narrative
    return f"{narrative} [risk excludes: {', '.join(unknown)} - no data]"


def run_risk(
    llm: LLM | None,
    conn: sqlite3.Connection,
    run_id: int,
    products: dict[str, Product],
    s: Settings,
) -> StageResult:
    degraded_error = ""
    all_cands = get_candidates(conn, run_id)
    own = {p.asin for p in list_proposals(conn) if p.run_id == run_id}
    best = _best_candidates(conn, all_cands, s, own)
    best_ids = {id(c) for c in best}
    top_ids = {id(c) for c in _top_n(conn, all_cands, s, own)}
    drops: list[tuple[str, str, str, str]] = []
    for c in all_cands:
        if id(c) in best_ids:
            continue
        reason = _drop_reason(conn, c, top_ids, s)
        if reason is not None:
            drops.append((c.asin, c.title, "risk", reason))
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
        if _fit_qty(1, c.supplier_cost, s) < 1:
            drops.append((c.asin, c.title, "risk",
                          f"unit cost {c.supplier_cost:.2f} exceeds max order value"
                          f" {s.max_order_value:.2f}"))
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
                    "factors": {
                        k: None if k in ra.unknown else v for k, v in ra.factors.items()
                    },
                    "unknown_factors": ra.unknown,
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

        qty = _fit_qty(suggested, c.supplier_cost, s)  # >= 1: one unit fits (checked above)
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
                narrative=_with_unknown(narrative, ra.unknown),
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
