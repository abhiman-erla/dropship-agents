from __future__ import annotations

import json
import sqlite3
from typing import Literal

from pydantic import BaseModel

from dropship.config import Settings
from dropship.db import (
    has_pending_proposal,
    recently_decided,
    record_dropped,
    save_candidates,
)
from dropship.llm import LLM, LLMBudgetExceeded, LLMUnusable
from dropship.margins import build_candidates, explain_drops, load_supplier_quotes
from dropship.models import Candidate, Product, StageResult
from dropship.sources import ProductSource

RESEARCH_MODEL = "claude-haiku-5-5"

RESEARCH_SYSTEM = (
    "You are a product researcher screening marketplace best-sellers for a small "
    "dropshipping business. The user message is a compact JSON list of products, each "
    "with: asin, title, price, margin_pct (gross margin after fees, as a fraction), "
    "rank (sales rank, lower is better), rating, reviews and offers (number of "
    "competing sellers).\n"
    "Rate each product for dropshipping potential as one of: promising (healthy "
    "margin, strong demand, manageable competition), weak (marginal or uncertain), "
    "or avoid (high risk: heavy competition, fragile, regulated or likely to be "
    "returned). Give a reason of at most 15 words.\n"
    "Return exactly one item per input asin, using the asin exactly as given. "
    "Do not add products that were not provided."
)


class Verdict(BaseModel):
    asin: str
    verdict: Literal["promising", "weak", "avoid"]
    reason: str


class Verdicts(BaseModel):
    items: list[Verdict]


def _user_payload(top: list[Candidate], products: dict[str, Product]) -> str:
    rows = []
    for c in top:
        p = products[c.asin]
        rows.append(
            {
                "asin": c.asin,
                "title": c.title[:60],
                "price": c.sell_price,
                "margin_pct": round(c.margin_pct, 2),
                "rank": p.sales_rank,
                "rating": p.rating,
                "reviews": p.review_count,
                "offers": p.offer_count,
            }
        )
    return json.dumps(rows, separators=(",", ":"))


def run_research(
    keepa: ProductSource,
    llm: LLM | None,
    conn: sqlite3.Connection,
    run_id: int,
    s: Settings,
) -> tuple[StageResult, dict[str, Product]]:
    asins: list[str] = []
    seen: set[str] = set()
    empty: list[str] = []
    for category in s.categories:
        listed = keepa.bestsellers(category)
        if not listed:
            empty.append(f"category {category} returned 0 products")
        for asin in listed[: s.max_asins_per_category]:
            if asin not in seen:
                seen.add(asin)
                asins.append(asin)
    fetched = keepa.products(asins) if asins else []
    products = {p.asin: p for p in fetched}

    quotes = load_supplier_quotes(s.supplier_quotes_path)
    candidates = build_candidates(fetched, quotes, s)
    # Pending or recently decided ASINs stay as (unreviewed) candidates for the report,
    # but must not take the LLM's top-N slots.
    eligible = [
        c for c in candidates
        if not has_pending_proposal(conn, c.asin)
        and not recently_decided(conn, c.asin, s.proposal_cooldown_days)
    ]
    top = eligible[: s.llm_top_n]

    status: Literal["ok", "degraded"] = "ok"
    error_types: list[str] = []
    messages: list[str] = []
    if empty:
        status = "degraded"
        error_types.append("EmptyCategory")
        messages.append("; ".join(empty))
    if top:
        if llm is None:
            status = "degraded"
            error_types.append("NoLLM")
            messages.append("no LLM client configured")
        else:
            try:
                verdicts = llm.parse(
                    model=RESEARCH_MODEL,
                    system=RESEARCH_SYSTEM,
                    user=_user_payload(top, products),
                    schema=Verdicts,
                    effort="low",
                )
                by_asin = {v.asin: v for v in verdicts.items}  # type: ignore[attr-defined]
                for c in top:
                    v = by_asin.get(c.asin)
                    if v is not None:
                        c.llm_verdict, c.llm_reason = v.verdict, v.reason
            except (LLMBudgetExceeded, LLMUnusable) as e:
                status = "degraded"
                error_types.append(type(e).__name__)
                messages.append(str(e))

    save_candidates(conn, run_id, candidates)
    record_dropped(
        conn, run_id,
        [("-", "-", "research", reason) for reason in empty]
        + [(a, t, "research", r) for a, t, r in explain_drops(fetched, quotes, s)],
    )
    return (
        StageResult(
            stage="research", status=status, attempts=1,
            error_type=", ".join(error_types), message="; ".join(messages),
        ),
        products,
    )
