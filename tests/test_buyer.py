import pytest

from dropship import db
from dropship.buyer import (FALLBACK_NARRATIVE, ApprovalError, RecordOnlyPurchaser, RiskNote,
                            approve, reject, run_risk)
from _india import india
from dropship.llm import LLMBudgetExceeded, LLMUnusable
from dropship.models import Candidate, Product, Proposal

S = india(categories=[1])


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
    s = india(categories=[1], max_order_value=10000.0, daily_spend_cap=15000.0)
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
    s = india(categories=[1], llm_top_n=1)
    run_risk(llm, conn, rid, {"B01": prod(), "B02": prod("B02")}, s)
    assert _asins(conn) == ["B01"] and len(llm.calls) == 1


def test_all_unreviewed_falls_back_to_top_n_by_score(tmp_path):
    cands = [cand(a, cost=100.0, verdict="unreviewed", score=sc)
             for a, sc in [("B01", 0.1), ("B02", 0.9), ("B03", 0.5)]]
    conn, rid = setup(tmp_path, cands)
    s = india(categories=[1], llm_top_n=2)
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


def test_cooldown_blocks_reproposal(tmp_path):
    conn, rid = setup(tmp_path, [cand(cost=100.0)])
    llm = FakeLLM()
    run_risk(llm, conn, rid, {"B01": prod()}, S)
    [p] = db.list_proposals(conn)
    approve(conn, p.id, S, RecordOnlyPurchaser())
    rid2 = db.start_run(conn)
    db.save_candidates(conn, rid2, [cand(cost=100.0)])
    n = len(llm.calls)
    run_risk(llm, conn, rid2, {"B01": prod()}, S)
    assert len(db.list_proposals(conn)) == 1 and len(llm.calls) == n
    [d] = db.get_dropped(conn, rid2)
    assert d["stage"] == "risk" and d["reason"] == "cooldown: decided within 30 days"


def test_risk_records_over_risk_and_verdict_drops(tmp_path):
    conn, rid = setup(tmp_path, [cand("B01"), cand("B02", verdict="weak"),
                                 cand("B03", verdict="avoid"), cand("B04")])
    hot = prod("B01", price=1700.0, sales_rank=30000, offer_count=15, rating=3.0)
    run_risk(FakeLLM(), conn, rid, {"B01": hot, "B04": prod("B04")}, S)
    drops = {d["asin"]: d["reason"] for d in db.get_dropped(conn, rid)}
    assert drops["B01"].startswith("risk ") and " > max 60" in drops["B01"]
    assert drops["B02"] == "verdict weak" and drops["B03"] == "verdict avoid"
    assert "B04" not in drops


def _canopy_like(asin="B01"):
    return prod(asin, price_avg90=None, sales_rank_avg90=None, offer_count=None)


def test_risk_payload_marks_unknown_factors_null(tmp_path):
    import json
    from dropship.buyer import RISK_SYSTEM
    conn, rid = setup(tmp_path, [cand(cost=100.0)])
    llm = FakeLLM()
    run_risk(llm, conn, rid, {"B01": _canopy_like()}, S)
    user = json.loads(llm.calls[0]["user"])
    assert user["unknown_factors"] == ["price_volatility", "rank_volatility", "competition"]
    assert user["factors"]["price_volatility"] is None
    assert user["factors"]["rank_volatility"] is None
    assert user["factors"]["competition"] is None
    assert user["factors"]["low_rating"] == 0
    assert "not that the risk is low" in RISK_SYSTEM


def test_narrative_suffix_for_unknown_factors(tmp_path):
    conn, rid = setup(tmp_path, [cand(cost=100.0)])
    run_risk(FakeLLM(), conn, rid, {"B01": _canopy_like()}, S)
    [p] = db.list_proposals(conn)
    assert p.narrative == (
        "fine [risk excludes: price_volatility, rank_volatility, competition - no data]")


def test_narrative_suffix_lists_only_missing_factors(tmp_path):
    conn, rid = setup(tmp_path, [cand(cost=100.0)])
    run_risk(FakeLLM(), conn, rid, {"B01": prod(offer_count=None)}, S)
    [p] = db.list_proposals(conn)
    assert p.narrative == "fine [risk excludes: competition - no data]"


def test_fallback_narrative_gets_suffix_too(tmp_path):
    conn, rid = setup(tmp_path, [cand(cost=100.0, verdict="unreviewed")])
    run_risk(None, conn, rid, {"B01": _canopy_like()}, S)
    [p] = db.list_proposals(conn)
    assert p.narrative == (FALLBACK_NARRATIVE
                           + " [risk excludes: price_volatility, rank_volatility,"
                           " competition - no data]")


def test_no_suffix_when_all_inputs_present(tmp_path):
    import json
    conn, rid = setup(tmp_path, [cand(cost=100.0)])
    llm = FakeLLM()
    run_risk(llm, conn, rid, {"B01": prod()}, S)
    [p] = db.list_proposals(conn)
    assert p.narrative == "fine"
    user = json.loads(llm.calls[0]["user"])
    assert user["unknown_factors"] == [] and None not in user["factors"].values()


def _decided(conn, rid, asin, status="approved_manual"):
    pid = _pending(conn, rid, asin, 1.0)
    if status != "pending":
        db.set_proposal_status(conn, pid, status)


def test_fallback_skips_pending_and_decided(tmp_path):
    cands = [cand(a, cost=100.0, verdict="unreviewed", score=sc)
             for a, sc in [("B01", 0.9), ("B02", 0.8), ("B03", 0.7), ("B04", 0.6),
                           ("B05", 0.5)]]
    conn, rid = setup(tmp_path, cands)
    earlier = db.start_run(conn)
    _decided(conn, earlier, "B01")
    _decided(conn, earlier, "B02", "pending")
    s = india(categories=[1], llm_top_n=2)
    run_risk(FakeLLM(), conn, rid, {c.asin: prod(c.asin) for c in cands}, s)
    new = sorted(p.asin for p in db.list_proposals(conn) if p.run_id == rid)
    assert new == ["B03", "B04"]
    drops = {d["asin"]: d["reason"] for d in db.get_dropped(conn, rid)}
    assert drops == {"B01": "cooldown: decided within 30 days",
                     "B05": "not in top 2 by score"}


def test_top_n_drop_reason_when_verdicts_exist(tmp_path):
    conn, rid = setup(tmp_path, [cand("B01", cost=100.0, score=0.9),
                                 cand("B02", verdict="weak", score=0.8),
                                 cand("B03", verdict="unreviewed", score=0.1)])
    s = india(categories=[1], llm_top_n=2)
    run_risk(FakeLLM(), conn, rid, {a: prod(a) for a in ("B01", "B02", "B03")}, s)
    drops = {d["asin"]: d["reason"] for d in db.get_dropped(conn, rid)}
    assert drops == {"B02": "verdict weak", "B03": "not in top 2 by score"}


def test_unit_cost_over_max_order_value_recorded(tmp_path):
    conn, rid = setup(tmp_path, [cand(cost=6000.0)])
    llm = FakeLLM()
    run_risk(llm, conn, rid, {"B01": prod()}, S)
    assert db.list_proposals(conn) == [] and llm.calls == []
    [d] = db.get_dropped(conn, rid)
    assert d["stage"] == "risk"
    assert d["reason"] == "unit cost 6000.00 exceeds max order value 5000.00"


def test_llm_omitted_verdict_keeps_unreviewed_reason(tmp_path):
    # B02 was within the top N sent to the LLM, but the LLM returned no verdict for it.
    conn, rid = setup(tmp_path, [cand("B01", cost=100.0, score=0.9),
                                 cand("B02", verdict="unreviewed", score=0.8)])
    s = india(categories=[1], llm_top_n=2)
    run_risk(FakeLLM(), conn, rid, {a: prod(a) for a in ("B01", "B02")}, s)
    [d] = db.get_dropped(conn, rid)
    assert (d["asin"], d["reason"]) == ("B02", "verdict unreviewed")


def test_fallback_retry_in_same_run_does_not_widen_top_n(tmp_path):
    # A retried risk stage must not treat this run's own new proposals as "pending
    # elsewhere" and move on to the next candidates.
    cands = [cand(a, cost=100.0, verdict="unreviewed", score=sc)
             for a, sc in [("B01", 0.9), ("B02", 0.8), ("B03", 0.7)]]
    conn, rid = setup(tmp_path, cands)
    s = india(categories=[1], llm_top_n=2)
    products = {c.asin: prod(c.asin) for c in cands}
    run_risk(FakeLLM(), conn, rid, products, s)
    run_risk(FakeLLM(), conn, rid, products, s)
    assert _asins(conn) == ["B01", "B02"]
