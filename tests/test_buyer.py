import pytest

from dropship import db
from dropship.buyer import (FALLBACK_NARRATIVE, ApprovalError, RecordOnlyPurchaser, RiskNote,
                            approve, reject, run_risk)
from dropship.config import Settings
from dropship.llm import LLMBudgetExceeded, LLMUnusable
from dropship.models import Candidate, Product, Proposal

S = Settings(categories=[1])


class FakeLLM:
    def __init__(self, qty=5, exc=None):
        self.calls, self.qty, self.exc = [], qty, exc

    def parse(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return RiskNote(narrative="fine", suggested_qty=self.qty)


class CountingPurchaser(RecordOnlyPurchaser):
    def __init__(self):
        self.n = 0

    def place(self, p):
        self.n += 1
        return super().place(p)


def cand(asin="B01", cost=1200.0, verdict="promising", score=1.0):
    return Candidate(asin=asin, title="t", sell_price=2500.0, supplier_cost=cost,
                     cost_source="quoted", margin=1000.0, margin_pct=0.45, score=score,
                     llm_verdict=verdict)


def prod(asin="B01", **kw):
    base = dict(asin=asin, title="t", price=1000.0, price_avg90=1000.0, sales_rank=10000,
                sales_rank_avg90=10000, rating=4.5, review_count=100, offer_count=3)
    base.update(kw)
    return Product(**base)


def setup(tmp_path, cands):
    conn = db.connect(tmp_path / "t.db")
    rid = db.start_run(conn)
    db.save_candidates(conn, rid, cands)
    return conn, rid


def test_low_risk_creates_pending_proposal(tmp_path):
    conn, rid = setup(tmp_path, [cand(cost=100.0)])
    llm = FakeLLM(qty=50)
    r = run_risk(llm, conn, rid, {"B01": prod()}, S)
    assert r.status == "ok" and r.stage == "risk"
    assert len(llm.calls) == 1
    assert llm.calls[0]["model"] == "claude-opus-5-5" and llm.calls[0]["fallbacks"] is True
    [p] = db.list_proposals(conn)
    assert p.status == "pending" and p.qty == min(50, 20) and p.total == 2000.0


def test_high_risk_no_proposal(tmp_path):
    conn, rid = setup(tmp_path, [cand()])
    llm = FakeLLM()
    hot = prod(price=1700.0, sales_rank=30000, offer_count=15, rating=3.0)
    run_risk(llm, conn, rid, {"B01": hot}, S)
    assert db.list_proposals(conn) == [] and llm.calls == []


def test_avoid_verdict_skipped(tmp_path):
    conn, rid = setup(tmp_path, [cand(verdict="avoid")])
    llm = FakeLLM()
    run_risk(llm, conn, rid, {"B01": prod()}, S)
    assert db.list_proposals(conn) == [] and llm.calls == []


def test_missing_product_skipped(tmp_path):
    conn, rid = setup(tmp_path, [cand()])
    llm = FakeLLM()
    run_risk(llm, conn, rid, {}, S)
    assert db.list_proposals(conn) == [] and llm.calls == []


def test_no_duplicate_pending_proposal(tmp_path):
    conn, rid = setup(tmp_path, [cand()])
    llm = FakeLLM(qty=2)
    run_risk(llm, conn, rid, {"B01": prod()}, S)
    run_risk(llm, conn, rid, {"B01": prod()}, S)
    assert len(db.list_proposals(conn, "pending")) == 1
    assert len(llm.calls) == 1


def test_qty_shrinks_to_order_cap(tmp_path):
    conn, rid = setup(tmp_path, [cand(cost=1200.0)])
    run_risk(FakeLLM(qty=10), conn, rid, {"B01": prod()}, S)
    assert db.list_proposals(conn)[0].qty == 4


def test_unit_too_expensive_skipped(tmp_path):
    conn, rid = setup(tmp_path, [cand(cost=6000.0)])
    run_risk(FakeLLM(), conn, rid, {"B01": prod()}, S)
    assert db.list_proposals(conn) == []


def test_llm_unavailable_degrades(tmp_path):
    conn, rid = setup(tmp_path, [cand("B01"), cand("B02")])
    llm = FakeLLM(exc=LLMBudgetExceeded("x"))
    r = run_risk(llm, conn, rid, {"B01": prod(), "B02": prod("B02")}, S)
    assert r.status == "degraded" and r.error_type == "LLMBudgetExceeded"
    ps = db.list_proposals(conn)
    assert len(ps) == 2
    assert all(p.qty == 1 and p.status == "pending" and "LLM unavailable" in p.narrative
               for p in ps)


def _pending(conn, rid, asin, total):
    return db.insert_proposal(conn, Proposal(id=None, run_id=rid, asin=asin, qty=1,
                              unit_cost=total, total=total, risk_score=10, narrative="n",
                              status="pending"))


def test_approve_happy(tmp_path):
    conn, rid = setup(tmp_path, [])
    pid = _pending(conn, rid, "B01", 1000.0)
    assert approve(conn, pid, S, RecordOnlyPurchaser()) == f"manual-{pid}"
    assert db.get_proposal(conn, pid).status == "approved_manual"


def test_approve_not_found(tmp_path):
    conn, rid = setup(tmp_path, [])
    with pytest.raises(ApprovalError, match="not found"):
        approve(conn, 99, S, RecordOnlyPurchaser())


def test_approve_twice_refused(tmp_path):
    conn, rid = setup(tmp_path, [])
    pid = _pending(conn, rid, "B01", 1000.0)
    pur = CountingPurchaser()
    approve(conn, pid, S, pur)
    with pytest.raises(ApprovalError, match="not pending"):
        approve(conn, pid, S, pur)
    assert pur.n == 1
    pid2 = _pending(conn, rid, "B02", 1000.0)
    reject(conn, pid2)
    with pytest.raises(ApprovalError):
        approve(conn, pid2, S, pur)
    with pytest.raises(ApprovalError):
        reject(conn, pid2)
    assert pur.n == 1


def test_approve_over_order_value_refused(tmp_path):
    conn, rid = setup(tmp_path, [])
    pid = _pending(conn, rid, "B01", 5001.0)
    pur = CountingPurchaser()
    with pytest.raises(ApprovalError, match="max order value"):
        approve(conn, pid, S, pur)
    assert pur.n == 0 and db.get_proposal(conn, pid).status == "pending"


def test_daily_cap_counts_prior_approvals(tmp_path):
    s = Settings(categories=[1], max_order_value=10000.0, daily_spend_cap=15000.0)
    conn, rid = setup(tmp_path, [])
    a = _pending(conn, rid, "B01", 9000.0)
    b = _pending(conn, rid, "B02", 7000.0)
    pur = CountingPurchaser()
    approve(conn, a, s, pur)
    with pytest.raises(ApprovalError, match="daily spend cap"):
        approve(conn, b, s, pur)
    assert pur.n == 1 and db.get_proposal(conn, b).status == "pending"


def _asins(conn):
    return sorted(p.asin for p in db.list_proposals(conn))


def test_weak_verdict_skipped(tmp_path):
    conn, rid = setup(tmp_path, [cand("B01", verdict="weak"), cand("B02", cost=100.0)])
    llm = FakeLLM()
    run_risk(llm, conn, rid, {"B01": prod(), "B02": prod("B02")}, S)
    assert _asins(conn) == ["B02"] and len(llm.calls) == 1


def test_unreviewed_skipped_when_verdicts_exist(tmp_path):
    # B02 ranked beyond the LLM's top-n, so it stayed unreviewed while B01 was judged.
    conn, rid = setup(tmp_path, [cand("B01", cost=100.0, score=2.0),
                                 cand("B02", cost=100.0, verdict="unreviewed", score=1.0)])
    llm = FakeLLM()
    s = Settings(categories=[1], llm_top_n=1)
    run_risk(llm, conn, rid, {"B01": prod(), "B02": prod("B02")}, s)
    assert _asins(conn) == ["B01"] and len(llm.calls) == 1


def test_all_unreviewed_falls_back_to_top_n_by_score(tmp_path):
    cands = [cand(a, cost=100.0, verdict="unreviewed", score=sc)
             for a, sc in [("B01", 0.1), ("B02", 0.9), ("B03", 0.5)]]
    conn, rid = setup(tmp_path, cands)
    s = Settings(categories=[1], llm_top_n=2)
    run_risk(FakeLLM(), conn, rid, {c.asin: prod(c.asin) for c in cands}, s)
    assert _asins(conn) == ["B02", "B03"]


def test_no_llm_creates_deterministic_proposal(tmp_path):
    conn, rid = setup(tmp_path, [cand(cost=100.0, verdict="unreviewed")])
    r = run_risk(None, conn, rid, {"B01": prod()}, S)
    assert r.status == "degraded" and r.error_type == "LLMUnusable"
    [p] = db.list_proposals(conn)
    assert p.qty == 1 and p.narrative == FALLBACK_NARRATIVE and p.status == "pending"


def test_llm_unusable_creates_deterministic_proposal(tmp_path):
    conn, rid = setup(tmp_path, [cand(cost=100.0)])
    llm = FakeLLM(qty=5, exc=LLMUnusable("refused"))
    r = run_risk(llm, conn, rid, {"B01": prod()}, S)
    assert r.status == "degraded" and r.error_type == "LLMUnusable"
    assert len(llm.calls) == 1
    [p] = db.list_proposals(conn)
    assert p.qty == 1 and p.narrative == FALLBACK_NARRATIVE and p.total == 100.0
