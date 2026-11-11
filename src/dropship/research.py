from __future__ import annotations

import json
import sqlite3
from typing import Literal

from pydantic import BaseModel

from dropship.config import Settings
from dropship.db import save_candidates
from dropship.keepa_client import KeepaClient
from dropship.llm import LLM, LLMBudgetExceeded, LLMUnusable
from dropship.margins import build_candidates, load_supplier_quotes
from dropship.models import Candidate, Product, StageResult

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
    keepa: KeepaClient,
    llm: LLM | None,
    conn: sqlite3.Connection,
    run_id: int,
    s: Settings,
) -> tuple[StageResult, dict[str, Product]]:
    asins: list[str] = []
    seen: set[str] = set()
    for category in s.categories:
        for asin in keepa.bestsellers(category)[: s.max_asins_per_category]:
            if asin not in seen:
                seen.add(asin)
                asins.append(asin)
    fetched = keepa.products(asins) if asins else []
    products = {p.asin: p for p in fetched}

    quotes = load_supplier_quotes(s.supplier_quotes_path)
    candidates = build_candidates(fetched, quotes, s)
    top = candidates[: s.llm_top_n]

    status: Literal["ok", "degraded"] = "ok"
    error_type = message = ""
    if top:
        if llm is None:
            status, error_type, message = "degraded", "NoLLM", "no LLM client configured"
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
                status, error_type, message = "degraded", type(e).__name__, str(e)

    save_candidates(conn, run_id, candidates)
    return (
        StageResult(
            stage="research", status=status, attempts=1, error_type=error_type, message=message
        ),
        products,
    )
