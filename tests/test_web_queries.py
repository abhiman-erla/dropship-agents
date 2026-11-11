import sqlite3
from datetime import date

import pytest

from dropship import db
from dropship.config import Settings
from dropship.models import Candidate, Proposal, StageResult
from dropship.web import queries as q


def _settings(tmp_path, **kw):
    return Settings(
        categories=["bestsellers_kitchen"],
        db_path=tmp_path / "d.db",
        supplier_quotes_path=tmp_path / "q.csv",
        **kw,
    )


def _cand(asin, title="Thing", source="estimated", price=30.0, verdict="promising"):
    return Candidate(
        asin=asin, title=title, sell_price=price, supplier_cost=10.0, cost_source=source,
        margin=12.0, margin_pct=0.4, score=0.7, llm_verdict=verdict, llm_reason="ok",
    )


def _prop(run_id, asin, total=24.49, status="pending"):
    return Proposal(
        id=None, run_id=run_id, asin=asin, qty=2, unit_cost=total / 2, total=total,
        risk_score=20, narrative="fine", status=status,
    )


def _stage(conn, run_id, stage, status="ok", error_type="", message=""):
    db.record_stage(
        conn, run_id,
        StageResult(stage=stage, status=status, attempts=1, error_type=error_type,
                    message=message),
        "2026-11-11T10:00:00", "2026-11-11T10:00:05",
    )


def _ro(s, conn):
    conn.close()
    return q.open_readonly(s.db_path)


def test_open_readonly_missing_returns_none(tmp_path):
    assert q.open_readonly(tmp_path / "nope.db") is None


def test_open_readonly_rejects_writes(tmp_path):
    s = _settings(tmp_path)
    ro = _ro(s, db.connect(s.db_path))
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO runs (started_at, status) VALUES ('x', 'ok')")


def test_open_readonly_survives_hash_in_path(tmp_path):
    path = tmp_path / "proj#1" / "t.db"
    path.parent.mkdir()
    db.connect(path).close()
    before = sorted(p.name for p in tmp_path.iterdir())
    ro = q.open_readonly(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO runs (started_at, status) VALUES ('x', 'ok')")
    finally:
        ro.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_open_readonly_percent_folder_opens_that_file(tmp_path):
    for folder, marker in (("a%20b", "percent"), ("a b", "space")):
        (tmp_path / folder).mkdir()
        conn = sqlite3.connect(tmp_path / folder / "t.db")
        conn.execute("CREATE TABLE m (v TEXT)")
        conn.execute("INSERT INTO m VALUES (?)", (marker,))
        conn.commit()
        conn.close()
    ro = q.open_readonly(tmp_path / "a%20b" / "t.db")
    try:
        assert ro.execute("SELECT v FROM m").fetchone()[0] == "percent"
    finally:
        ro.close()


def test_dashboard_tiles(tmp_path):
    s = _settings(tmp_path)
    conn = db.connect(s.db_path)
    run = db.start_run(conn)
    db.save_candidates(conn, run, [_cand("A1"), _cand("A2"), _cand("A3")])
    _stage(conn, run, "research", "degraded", "KeepaError", "partial")
    _stage(conn, run, "risk")
    db.finish_run(conn, run, "degraded")
    db.insert_proposal(conn, _prop(run, "A1"))
    db.insert_proposal(conn, _prop(run, "A2"))
    pid = db.insert_proposal(conn, _prop(run, "A3"))
    db.set_proposal_status(conn, pid, "approved_manual")
    db.record_source_requests(conn, run, "canopy", 3)
    db.record_llm_usage(conn, run, "m", 600, 300)
    today = date.today()
    ro = _ro(s, conn)
    d = q.dashboard(ro, s, today)
    assert d.pending_count == 2
    assert [o.asin for o in d.pending] == ["A2", "A1"]
    assert d.approved_today == 24.49
    assert d.daily_cap == s.daily_spend_cap
    assert d.requests_this_month == 3
    assert d.free_quota == 100
    assert d.tokens_today == 900
    assert d.last_run.id == run
    assert [e.stage for e in d.last_errors] == ["research"]


def test_dashboard_free_quota_none_for_keepa(tmp_path):
    s = Settings(data_source="keepa", categories=[1], db_path=tmp_path / "d.db",
                 supplier_quotes_path=tmp_path / "q.csv")
    ro = _ro(s, db.connect(s.db_path))
    assert q.dashboard(ro, s, date.today()).free_quota is None


def test_dashboard_empty_db(tmp_path):
    s = _settings(tmp_path)
    ro = _ro(s, db.connect(s.db_path))
    d = q.dashboard(ro, s, date.today())
    assert d.last_run is None
    assert d.pending_count == 0 and d.pending == [] and d.last_errors == []
    assert d.approved_today == 0 and d.requests_this_month == 0 and d.tokens_today == 0


def test_orders_filtered_and_labelled(tmp_path):
    s = _settings(tmp_path)
    conn = db.connect(s.db_path)
    run = db.start_run(conn)
    db.save_candidates(conn, run, [_cand("A1", "Nice\nmug"), _cand("A2", source="quoted")])
    db.insert_proposal(conn, _prop(run, "A1"))
    db.insert_proposal(conn, _prop(run, "A2", status="rejected"))
    ro = _ro(s, conn)
    pend = q.orders(ro, "pending")
    assert [o.asin for o in pend] == ["A1"]
    assert pend[0].title == "Nice mug"
    assert pend[0].cost_label == "estimated cost"
    assert pend[0].amazon_url == "https://www.amazon.com/dp/A1"
    assert [o.asin for o in q.orders(ro, "rejected")] == ["A2"]
    assert q.orders(ro, "rejected")[0].cost_label == "quoted"
    assert q.orders(ro, "approved") == []
    with pytest.raises(ValueError):
        q.orders(ro, "bogus")


def test_run_summary_counts(tmp_path):
    s = _settings(tmp_path)
    conn = db.connect(s.db_path)
    run = db.start_run(conn)
    db.save_candidates(conn, run, [_cand("C1"), _cand("C2"), _cand("C3")])
    db.record_dropped(conn, run, [
        ("D1", "t", "research", "price out of range"),
        ("D2", "t", "research", "rank too high"),
        ("D3", "t", "research", "rating too low"),
        ("D4", "t", "research", "missing price"),
        ("-", "-", "research", "empty category"),
        ("C1", "t", "risk", "risk too high"),
    ])
    db.insert_proposal(conn, _prop(run, "C2"))
    db.finish_run(conn, run, "ok")
    ro = _ro(s, conn)
    r = q.runs(ro)[0]
    assert (r.fetched, r.dropped, r.proposed) == (7, 5, 1)
    assert r.duration_s is not None and r.duration_s >= 0


def test_runs_newest_first_and_running_has_no_duration(tmp_path):
    s = _settings(tmp_path)
    conn = db.connect(s.db_path)
    db.start_run(conn)
    db.start_run(conn)
    ro = _ro(s, conn)
    rs = q.runs(ro)
    assert [r.id for r in rs] == [2, 1]
    assert rs[0].duration_s is None


def test_run_detail_reason_filter(tmp_path):
    s = _settings(tmp_path)
    conn = db.connect(s.db_path)
    run = db.start_run(conn)
    db.save_candidates(conn, run, [_cand("C1")])
    _stage(conn, run, "research")
    db.record_dropped(conn, run, [
        ("D1", "t", "research", "blocked brand: Acme"),
        ("D2", "t", "research", "blocked brand: Zed"),
        ("D3", "t", "research", "rank too high"),
    ])
    db.record_llm_usage(conn, run, "m", 100, 50)
    db.record_llm_usage(conn, run, "m", 10, 5)
    ro = _ro(s, conn)
    d = q.run_detail(ro, run, "BRAND")
    assert [x["asin"] for x in d.dropped] == ["D1", "D2"]
    assert len(q.run_detail(ro, run, None).dropped) == 3
    assert d.tokens == 165
    assert [c.asin for c in d.candidates] == ["C1"]
    assert [st.stage for st in d.stages] == ["research"]
    assert d.summary.id == run
    assert q.run_detail(ro, 999, None) is None


def test_products_outcome_priority(tmp_path):
    s = _settings(tmp_path)
    conn = db.connect(s.db_path)
    r1 = db.start_run(conn)
    db.save_candidates(conn, r1, [
        _cand("P1", "Sourdough Starter Kit"), _cand("P2", "Plain candidate"),
        _cand("P3", "Pending thing"),
    ])
    db.insert_proposal(conn, _prop(r1, "P1", status="approved_manual"))
    db.insert_proposal(conn, _prop(r1, "P3"))
    r2 = db.start_run(conn)
    db.save_candidates(conn, r2, [_cand("P2", "Plain candidate", price=33.0), _cand("P3")])
    db.record_dropped(conn, r2, [
        ("P1", "Sourdough Starter Kit", "research", "cooldown: decided within 30 days"),
        ("-", "-", "research", "empty category"),
    ])
    db.insert_proposal(conn, _prop(r2, "P3"))
    ro = _ro(s, conn)
    rows = {p.asin: p for p in q.products(ro, None)}
    assert set(rows) == {"P1", "P2", "P3"}
    assert rows["P1"].outcome == "dropped: cooldown: decided within 30 days"
    assert rows["P1"].last_run_id == 2
    assert rows["P1"].price == 30.0 and rows["P1"].verdict == "promising"
    assert rows["P2"].outcome == "candidate" and rows["P2"].price == 33.0
    assert rows["P3"].outcome == "pending"
    assert rows["P2"].amazon_url == "https://www.amazon.com/dp/P2"
    assert [p.asin for p in q.products(ro, None)] == ["P1", "P2", "P3"]
    assert [p.asin for p in q.products(ro, "SOURDOUGH")] == ["P1"]
    assert [p.asin for p in q.products(ro, "p3")] == ["P3"]


def test_settings_lines_and_quotes(tmp_path):
    s = _settings(tmp_path, blocked_brands=["Acme"])
    lines = q.settings_lines(s)
    assert lines[1] == "Products must cost $15–60"
    assert lines[0] == "Data source: canopy (bestsellers_kitchen)"
    assert lines[3].startswith("Minimum margin 25% (fee 15%, shipping $5, estimated cost 35%")
    assert lines[-1] == "Blocked brands: Acme"
    assert len(lines) == 8
    assert q.settings_lines(_settings(tmp_path))[-1] == "Blocked brands: none"
    (tmp_path / "q.csv").write_text("asin,unit_cost\nB2,5\nA1,3.5\n", encoding="utf-8")
    assert q.quotes(s) == [("A1", 3.5), ("B2", 5.0)]
