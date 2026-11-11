from dropship import db
from dropship.models import Candidate, Proposal


def _cand(asin: str, score: float) -> Candidate:
    return Candidate(
        asin=asin, title=f"Item {asin}", sell_price=2000.0, supplier_cost=1200.0,
        cost_source="quoted", margin=800.0, margin_pct=0.4, score=score,
        llm_verdict="promising", llm_reason="ok",
    )


def _prop(run_id: int, asin: str, total: float) -> Proposal:
    return Proposal(id=None, run_id=run_id, asin=asin, qty=1, unit_cost=total,
                    total=total, risk_score=10, narrative="n", status="pending")


def test_candidates_roundtrip(tmp_path):
    conn = db.connect(tmp_path / "sub" / "t.db")
    run_id = db.start_run(conn)
    cands = [_cand("B01", 0.9), _cand("B02", 0.5)]
    db.save_candidates(conn, run_id, cands)
    assert db.get_candidates(conn, run_id) == cands


def test_pending_flag(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    run_id = db.start_run(conn)
    pid = db.insert_proposal(conn, _prop(run_id, "B0X", 100.0))
    assert db.has_pending_proposal(conn, "B0X") is True
    db.set_proposal_status(conn, pid, "rejected")
    assert db.has_pending_proposal(conn, "B0X") is False


def test_approved_total_today(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    run_id = db.start_run(conn)
    a = db.insert_proposal(conn, _prop(run_id, "B01", 1000.0))
    b = db.insert_proposal(conn, _prop(run_id, "B02", 2500.0))
    c = db.insert_proposal(conn, _prop(run_id, "B03", 999.0))
    db.set_proposal_status(conn, a, "approved_manual")
    db.set_proposal_status(conn, b, "approved_manual")
    db.set_proposal_status(conn, c, "rejected")
    assert db.approved_total_today(conn) == 3500.0
    p = db.get_proposal(conn, a)
    assert p is not None and p.status == "approved_manual"
    assert len(db.list_proposals(conn, "approved_manual")) == 2
    assert len(db.list_proposals(conn)) == 3


def test_llm_tokens_today(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    run_id = db.start_run(conn)
    db.record_llm_usage(conn, run_id, "m", 100, 50)
    db.record_llm_usage(conn, run_id, "m", 100, 50)
    assert db.llm_tokens_today(conn) == 300


def test_last_success(tmp_path):
    from datetime import datetime
    from dropship.models import StageResult
    conn = db.connect(tmp_path / "t.db")
    run_id = db.start_run(conn)
    assert db.last_success(conn, "research") is None
    db.record_stage(conn, run_id, StageResult(stage="research", status="failed", attempts=3),
                    "2026-01-01T00:00:00", "2026-01-01T00:01:00")
    assert db.last_success(conn, "research") is None
    db.record_stage(conn, run_id, StageResult(stage="research", status="ok", attempts=1),
                    "2026-01-02T00:00:00", "2026-01-02T00:01:00")
    db.record_stage(conn, run_id, StageResult(stage="research", status="degraded", attempts=1),
                    "2026-01-03T00:00:00", "2026-01-03T00:01:00")
    assert db.last_success(conn, "research") == datetime(2026, 1, 3, 0, 1)
