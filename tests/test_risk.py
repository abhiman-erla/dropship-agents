from _india import india
from dropship.models import Candidate, Product
from dropship.risk import risk_score

S = india(categories=[1])


def cand(margin_pct=0.30, source="estimated"):
    return Candidate(asin="B01", title="t", sell_price=2000.0, supplier_cost=1200.0,
                     cost_source=source, margin=800.0, margin_pct=margin_pct, score=1.0)


def prod(**kw):
    base = dict(asin="B01", title="t", price=1100.0, price_avg90=1000.0, sales_rank=12000,
                sales_rank_avg90=10000, rating=4.0, review_count=100, offer_count=6)
    base.update(kw)
    return Product(**base)


def test_risk_factors_example():
    ra = risk_score(cand(), prod(), S)
    assert ra.factors == {"price_volatility": 10, "rank_volatility": 10, "competition": 8,
                          "low_rating": 10, "thin_margin": 5, "estimated_cost": 10}
    assert ra.risk_score == 53


def test_risk_capped_100():
    p = prod(price=5000.0, sales_rank=90000, offer_count=100, rating=1.0)
    ra = risk_score(cand(margin_pct=0.0), p, S)
    assert ra.factors["price_volatility"] == 25
    assert ra.risk_score == 100


def test_missing_inputs_are_zero():
    p = prod(price=None, price_avg90=None, sales_rank=None, rating=None, offer_count=None)
    ra = risk_score(cand(margin_pct=0.5, source="quoted"), p, S)
    assert ra.risk_score == 0
    assert ra.unknown == ["price_volatility", "rank_volatility", "competition"]


def test_unknown_factors_listed_when_inputs_missing():
    # Canopy-like: no 90-day averages and no offer count.
    p = prod(price_avg90=None, sales_rank_avg90=None, offer_count=None)
    ra = risk_score(cand(), p, S)
    assert ra.unknown == ["price_volatility", "rank_volatility", "competition"]
    assert ra.factors["price_volatility"] == 0 and ra.factors["competition"] == 0
    assert ra.risk_score == 25  # low_rating 10 + thin_margin 5 + estimated_cost 10


def test_no_unknown_factors_when_inputs_present():
    assert risk_score(cand(), prod(), S).unknown == []


def test_partial_unknown_factors():
    ra = risk_score(cand(), prod(offer_count=None), S)
    assert ra.unknown == ["competition"]
