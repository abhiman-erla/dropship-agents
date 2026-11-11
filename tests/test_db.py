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


def test_get_run(tmp_path):
    from dropship.models import StageResult  # noqa: F401
    conn = db.connect(tmp_path / "t.db")
    assert db.get_run(conn, 99) is None
    run_id = db.start_run(conn)
    assert db.get_run(conn, run_id)["status"] == "running"
    db.finish_run(conn, run_id, "degraded")
    row = db.get_run(conn, run_id)
    assert row["status"] == "degraded" and row["finished_at"] is not None


def test_get_stage_runs(tmp_path):
    from dropship.models import StageResult
    conn = db.connect(tmp_path / "t.db")
    run_id = db.start_run(conn)
    db.record_stage(conn, run_id, StageResult(stage="research", status="ok", attempts=2),
                    "2026-01-01T00:00:00", "2026-01-01T00:01:00")
    db.record_stage(conn, run_id, StageResult(stage="risk", status="failed", attempts=1,
                    error_type="ValueError", message="boom"),
                    "2026-01-01T00:01:00", "2026-01-01T00:02:00")
    rows = db.get_stage_runs(conn, run_id)
    assert [r["stage"] for r in rows] == ["research", "risk"]
    assert rows[0]["attempts"] == 2 and rows[1]["error_type"] == "ValueError"
    assert db.get_stage_runs(conn, run_id + 1) == []


def test_latest_run_id(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    assert db.latest_run_id(conn) is None
    db.start_run(conn)
    second = db.start_run(conn)
    assert db.latest_run_id(conn) == second


def test_get_candidate(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    r1 = db.start_run(conn)
    r2 = db.start_run(conn)
    db.save_candidates(conn, r1, [_cand("B01", 0.9), _cand("B02", 0.5)])
    assert db.get_candidate(conn, r1, "B02") == _cand("B02", 0.5)
    assert db.get_candidate(conn, r1, "B99") is None
    assert db.get_candidate(conn, r2, "B01") is None


def _decided_prop(conn, run_id, asin, status="approved_manual"):
    pid = db.insert_proposal(conn, _prop(run_id, asin, 10.0))
    if status != "pending":
        db.set_proposal_status(conn, pid, status)
    return pid


def test_recently_decided(tmp_path):
    from datetime import datetime, timedelta
    conn = db.connect(tmp_path / "t.db")
    run_id = db.start_run(conn)
    pid = _decided_prop(conn, run_id, "B01")
    assert db.recently_decided(conn, "B01", 30) is True
    assert db.recently_decided(conn, "B01", 0) is False
    assert db.recently_decided(conn, "B99", 30) is False
    old = (datetime.now() - timedelta(days=31)).isoformat()
    conn.execute("UPDATE proposals SET decided_at = ? WHERE id = ?", (old, pid))
    conn.commit()
    assert db.recently_decided(conn, "B01", 30) is False
    _decided_prop(conn, run_id, "B02", "pending")
    assert db.recently_decided(conn, "B02", 30) is False
    _decided_prop(conn, run_id, "B03", "rejected")
    assert db.recently_decided(conn, "B03", 30) is True


def test_dropped_roundtrip_and_schema_upgrade(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("DROP TABLE dropped")
    db.init_schema(conn)  # existing DBs without the table keep working
    run_id = db.start_run(conn)
    db.record_dropped(conn, run_id, [("B01", "T1", "research", "missing price"),
                                     ("B02", "T2", "risk", "verdict weak")])
    db.record_dropped(conn, run_id, [])
    assert db.get_dropped(conn, run_id) == [
        {"asin": "B01", "title": "T1", "stage": "research", "reason": "missing price"},
        {"asin": "B02", "title": "T2", "stage": "risk", "reason": "verdict weak"},
    ]
    assert db.get_dropped(conn, run_id + 1) == []
