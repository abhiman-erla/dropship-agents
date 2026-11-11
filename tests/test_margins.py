from pathlib import Path

import pytest

from _india import india
from dropship.config import Settings
from dropship.margins import (
    build_candidates,
    compute_margin,
    load_supplier_quotes,
    passes_filter,
    score,
)
from dropship.models import Product


def make_product(**kw) -> Product:
    base = dict(
        asin="A1",
        title="Thing",
        price=1000.0,
        price_avg90=1000.0,
        sales_rank=1000,
        sales_rank_avg90=1000,
        rating=4.5,
        review_count=100,
        offer_count=5,
    )
    base.update(kw)
    return Product(**base)


@pytest.fixture
def s() -> Settings:
    return india(categories=[1])


def test_margin_formula(s):
    margin, pct = compute_margin(1000.0, 350.0, s)
    assert margin == pytest.approx(440.0)
    assert pct == pytest.approx(0.44)


def test_estimated_cost_used_without_quote(s):
    [c] = build_candidates([make_product()], {}, s)
    assert c.cost_source == "estimated"
    assert c.supplier_cost == pytest.approx(350.0)


def test_quote_overrides(s):
    [c] = build_candidates([make_product()], {"A1": 200.0}, s)
    assert c.cost_source == "quoted"
    assert c.supplier_cost == pytest.approx(200.0)


def test_filter_skips_missing_price(s):
    assert passes_filter(make_product(price=None), s) == (False, "missing price")
    assert passes_filter(make_product(price=0), s) == (False, "missing price")
    assert build_candidates(
        [make_product(price=None), make_product(asin="A2", price=0)], {}, s
    ) == []


@pytest.mark.parametrize(
    "kw,reason",
    [
        ({"price": 100.0}, "price out of range"),
        ({"price": 5000.0}, "price out of range"),
        ({"sales_rank": None}, "missing sales rank"),
        ({"sales_rank": 60000}, "rank too high"),
        ({"rating": None}, "missing rating"),
        ({"rating": 3.5}, "rating too low"),
        ({"review_count": None}, "too few reviews"),
        ({"review_count": 10}, "too few reviews"),
        ({"offer_count": 16}, "too many offers"),
    ],
)
def test_filter_reasons(s, kw, reason):
    assert passes_filter(make_product(**kw), s) == (False, reason)


def test_filter_pass_and_none_offers(s):
    assert passes_filter(make_product(), s) == (True, "")
    assert passes_filter(make_product(offer_count=None), s) == (True, "")


def test_below_min_margin_dropped(s):
    # quote 800 on price 1000 -> margin 1000-800-150-60=-10 -> dropped
    assert build_candidates([make_product()], {"A1": 800.0}, s) == []
    # margin_pct 0.2: cost 590 -> 1000-590-150-60=200
    assert build_candidates([make_product()], {"A1": 590.0}, s) == []


def test_score_components(s):
    p = make_product(sales_rank=25000, rating=4.0)
    # margin_pct 0.3 -> 0.5 ; rank_norm 0.5 -> 0.15 ; rating_norm 0.5 -> 0.1
    assert score(p, 0.3, s) == pytest.approx(0.5 * 0.5 + 0.3 * 0.5 + 0.2 * 0.5)


def test_score_clipped(s):
    p = make_product(sales_rank=100000, rating=2.0)
    assert score(p, 0.9, s) == pytest.approx(0.5)
    p2 = make_product(sales_rank=0, rating=5.0)
    assert score(p2, 1.5, s) == pytest.approx(1.0)


def test_sorted_by_score(s):
    lo = make_product(asin="LO", sales_rank=40000, rating=4.0)
    hi = make_product(asin="HI", sales_rank=100, rating=4.9)
    out = build_candidates([lo, hi], {}, s)
    assert [c.asin for c in out] == ["HI", "LO"]
    assert out[0].score > out[1].score


def test_load_supplier_quotes(tmp_path: Path):
    assert load_supplier_quotes(tmp_path / "missing.csv") == {}
    f = tmp_path / "q.csv"
    f.write_text("asin,unit_cost\nA1,200.5\nA2,\nA3,abc\nA4, 10 \n", encoding="utf-8")
    assert load_supplier_quotes(f) == {"A1": 200.5, "A4": 10.0}


@pytest.mark.parametrize("bad", ["nan", "inf", "-5", "0"])
def test_load_supplier_quotes_rejects_bad_values(tmp_path: Path, bad):
    f = tmp_path / "q.csv"
    f.write_text(f"asin,unit_cost\nA1,{bad}\nA2,50\n", encoding="utf-8")
    assert load_supplier_quotes(f) == {"A2": 50.0}
