from __future__ import annotations

from dropship.config import Settings
from dropship.models import Candidate, Product, RiskAssessment


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def risk_score(c: Candidate, p: Product, s: Settings) -> RiskAssessment:
    unknown: list[str] = []

    price_vol = 0.0
    if p.price is not None and p.price_avg90:
        price_vol = min(25.0, abs(p.price - p.price_avg90) / p.price_avg90 * 100)
    else:
        unknown.append("price_volatility")

    rank_vol = 0.0
    if p.sales_rank is not None and p.sales_rank_avg90:
        rank_vol = min(
            20.0, max(0.0, (p.sales_rank - p.sales_rank_avg90) / p.sales_rank_avg90) * 50
        )
    else:
        unknown.append("rank_volatility")

    competition = 0.0
    if p.offer_count is not None:
        competition = min(20.0, p.offer_count / s.max_offer_count * 20)
    else:
        unknown.append("competition")

    low_rating = 0.0
    if p.rating is not None:
        low_rating = _clamp((4.5 - p.rating) * 20, 0.0, 15.0)

    thin_margin = _clamp((0.40 - c.margin_pct) * 50, 0.0, 10.0)
    estimated = 10.0 if c.cost_source == "estimated" else 0.0

    factors = {
        "price_volatility": round(price_vol),
        "rank_volatility": round(rank_vol),
        "competition": round(competition),
        "low_rating": round(low_rating),
        "thin_margin": round(thin_margin),
        "estimated_cost": round(estimated),
    }
    return RiskAssessment(
        asin=c.asin, risk_score=min(100, sum(factors.values())), factors=factors,
        unknown=unknown,
    )
