import json
from types import SimpleNamespace

import pytest

from dropship.config import Settings
from dropship.db import connect, get_candidates, record_llm_usage, start_run
from dropship.llm import LLM, LLMUnusable
from dropship.models import Product
from dropship.research import RESEARCH_SYSTEM, Verdict, Verdicts, run_research


def prod(asin, **kw):
    base = dict(
        asin=asin, title="T" * 80, price=1000.0, price_avg90=1000.0, sales_rank=1000,
        sales_rank_avg90=1000, rating=4.5, review_count=100, offer_count=5,
    )
    base.update(kw)
    return Product(**base)


class FakeKeepa:
    def __init__(self, by_cat, products):
        self.by_cat = by_cat
        self._products = products
        self.requested: list[list[str]] = []

    def bestsellers(self, category):
        return self.by_cat[category]

    def products(self, asins):
        self.requested.append(list(asins))
        return [self._products[a] for a in asins]


class FakeClient:
    def __init__(self, parsed):
        self.calls = []
        self.parsed = parsed
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(
            stop_reason="end_turn", parsed_output=self.parsed,
            usage=SimpleNamespace(input_tokens=50, output_tokens=10),
        )


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def settings(tmp_path, **kw):
    return Settings(categories=[1], supplier_quotes_path=tmp_path / "none.csv", **kw)


def setup(conn, tmp_path, parsed, **kw):
    products = {
        "B0A": prod("B0A"),
        "B0B": prod("B0B", rating=4.4),
        "B0C": prod("B0C", price=None),  # fails the filter
    }
    keepa = FakeKeepa({1: ["B0A", "B0B", "B0C"]}, products)
    run_id = start_run(conn)
    client = FakeClient(parsed)
    s = settings(tmp_path, **kw)
    return keepa, client, run_id, s, LLM(client, conn, run_id, s.daily_llm_token_budget)


def test_system_prompt_is_static():
    assert "promising" in RESEARCH_SYSTEM and "15 words" in RESEARCH_SYSTEM
    assert not any(ch.isdigit() for ch in RESEARCH_SYSTEM.replace("15", ""))


def test_research_happy_path(conn, tmp_path):
    parsed = Verdicts(items=[
        Verdict(asin="B0A", verdict="promising", reason="good margin"),
        Verdict(asin="B0B", verdict="weak", reason="meh"),
    ])
    keepa, client, run_id, s, llm = setup(conn, tmp_path, parsed)
    result, products = run_research(keepa, llm, conn, run_id, s)
    assert result.stage == "research" and result.status == "ok" and result.attempts == 1
    assert len(client.calls) == 1
    assert set(products) == {"B0A", "B0B", "B0C"}
    cands = {c.asin: c for c in get_candidates(conn, run_id)}
    assert set(cands) == {"B0A", "B0B"}
    assert cands["B0A"].llm_verdict == "promising" and cands["B0A"].llm_reason == "good margin"
    assert cands["B0B"].llm_verdict == "weak"
    payload = json.loads(client.calls[0]["messages"][0]["content"])
    assert set(payload[0]) == {"asin", "title", "price", "margin_pct", "rank", "rating", "reviews", "offers"}
    assert len(payload[0]["title"]) == 60
    assert client.calls[0]["model"] == "claude-haiku-5-5"
    assert client.calls[0]["output_config"] == {"effort": "low"}


def test_research_degraded_on_budget(conn, tmp_path):
    keepa, client, run_id, s, llm = setup(conn, tmp_path, None, daily_llm_token_budget=10)
    record_llm_usage(conn, run_id, "m", 10, 5)
    result, _ = run_research(keepa, llm, conn, run_id, s)
    assert result.status == "degraded" and result.error_type == "LLMBudgetExceeded"
    assert client.calls == []
    cands = get_candidates(conn, run_id)
    assert len(cands) == 2 and all(c.llm_verdict == "unreviewed" for c in cands)


def test_research_degraded_on_unusable(conn, tmp_path):
    keepa, client, run_id, s, llm = setup(conn, tmp_path, None)  # parsed_output None
    result, _ = run_research(keepa, llm, conn, run_id, s)
    assert result.status == "degraded" and result.error_type == LLMUnusable.__name__
    assert len(get_candidates(conn, run_id)) == 2


def test_research_degraded_without_llm(conn, tmp_path):
    keepa, _, run_id, s, _ = setup(conn, tmp_path, None)
    result, _ = run_research(keepa, None, conn, run_id, s)
    assert result.status == "degraded" and result.error_type == "NoLLM"
    assert len(get_candidates(conn, run_id)) == 2


def test_llm_extra_and_missing_asins(conn, tmp_path):
    parsed = Verdicts(items=[
        Verdict(asin="B0A", verdict="promising", reason="ok"),
        Verdict(asin="ZZZ", verdict="avoid", reason="unknown"),
    ])
    keepa, _, run_id, s, llm = setup(conn, tmp_path, parsed)
    run_research(keepa, llm, conn, run_id, s)
    cands = {c.asin: c for c in get_candidates(conn, run_id)}
    assert cands["B0A"].llm_verdict == "promising"
    assert cands["B0B"].llm_verdict == "unreviewed"
    assert "ZZZ" not in cands


def test_only_top_n_sent(conn, tmp_path):
    parsed = Verdicts(items=[Verdict(asin="B0A", verdict="promising", reason="ok")])
    keepa, client, run_id, s, llm = setup(conn, tmp_path, parsed, llm_top_n=1)
    run_research(keepa, llm, conn, run_id, s)
    assert len(json.loads(client.calls[0]["messages"][0]["content"])) == 1
    assert len(get_candidates(conn, run_id)) == 2


def test_asins_deduplicated_across_categories(conn, tmp_path):
    products = {"B0A": prod("B0A"), "B0B": prod("B0B")}
    keepa = FakeKeepa({1: ["B0A", "B0B"], 2: ["B0B", "B0A"]}, products)
    run_id = start_run(conn)
    s = Settings(categories=[1, 2], supplier_quotes_path=tmp_path / "none.csv")
    run_research(keepa, None, conn, run_id, s)
    assert keepa.requested == [["B0A", "B0B"]]


def test_zero_candidates_skips_llm(conn, tmp_path):
    keepa = FakeKeepa({1: ["B0C"]}, {"B0C": prod("B0C", price=None)})
    run_id = start_run(conn)
    client = FakeClient(None)
    s = settings(tmp_path)
    result, products = run_research(keepa, LLM(client, conn, run_id, 1000), conn, run_id, s)
    assert result.status == "ok" and client.calls == [] and "B0C" in products
    assert get_candidates(conn, run_id) == []
