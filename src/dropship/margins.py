from __future__ import annotations

import csv
from pathlib import Path

from dropship.config import Settings
from dropship.models import Candidate, Product


def load_supplier_quotes(path: Path) -> dict[str, float]:
    path = Path(path)
    if not path.exists():
        return {}
    quotes: dict[str, float] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            asin = (row.get("asin") or "").strip()
            raw = (row.get("unit_cost") or "").strip()
            if not asin or not raw:
                continue
            try:
                quotes[asin] = float(raw)
            except ValueError:
                continue
    return quotes


def passes_filter(p: Product, s: Settings) -> tuple[bool, str]:
    if p.price is None or p.price <= 0:
        return False, "missing price"
    if not (s.price_min <= p.price <= s.price_max):
        return False, "price out of range"
    if p.sales_rank is None:
        return False, "missing sales rank"
    if p.sales_rank > s.max_sales_rank:
        return False, "rank too high"
    if p.rating is None:
        return False, "missing rating"
    if p.rating < s.min_rating:
        return False, "rating too low"
    if p.review_count is None or p.review_count < s.min_reviews:
        return False, "too few reviews"
    if (p.offer_count or 0) > s.max_offer_count:
        return False, "too many offers"
    return True, ""


def compute_margin(
    sell_price: float, supplier_cost: float, s: Settings
) -> tuple[float, float]:
    margin = sell_price - supplier_cost - sell_price * s.fee_pct - s.shipping_cost
    return margin, margin / sell_price


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def score(p: Product, margin_pct: float, s: Settings) -> float:
    margin_norm = _clip01(margin_pct / 0.6)
    rank_norm = _clip01(1 - (p.sales_rank or 0) / s.max_sales_rank)
    rating_norm = _clip01(((p.rating or 0.0) - 3) / 2)
    return 0.5 * margin_norm + 0.3 * rank_norm + 0.2 * rating_norm


def build_candidates(
    products: list[Product], quotes: dict[str, float], s: Settings
) -> list[Candidate]:
    out: list[Candidate] = []
    for p in products:
        ok, _ = passes_filter(p, s)
        if not ok:
            continue
        assert p.price is not None
        if p.asin in quotes:
            cost, source = quotes[p.asin], "quoted"
        else:
            cost, source = p.price * s.est_cost_ratio, "estimated"
        margin, margin_pct = compute_margin(p.price, cost, s)
        if margin_pct < s.min_margin_pct:
            continue
        out.append(
            Candidate(
                asin=p.asin,
                title=p.title,
                sell_price=p.price,
                supplier_cost=cost,
                cost_source=source,
                margin=margin,
                margin_pct=margin_pct,
                score=score(p, margin_pct, s),
            )
        )
    out.sort(key=lambda c: c.score, reverse=True)
    return out
