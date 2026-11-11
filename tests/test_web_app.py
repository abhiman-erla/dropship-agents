import sqlite3

import pytest
from fastapi.testclient import TestClient

from dropship import db
from dropship.config import Settings
from dropship.models import Candidate, Proposal, StageResult
from dropship.web import queries
from dropship.web.app import create_app

EMPTY = r"No runs yet. Run .venv\Scripts\python -m dropship run in PowerShell."


def _settings(tmp_path):
    return Settings(
        categories=["bestsellers_kitchen"],
        db_path=tmp_path / "d.db",
        supplier_quotes_path=tmp_path / "q.csv",
    )


def _cand(asin, title="Thing", source="estimated"):
    return Candidate(
        asin=asin, title=title, sell_price=30.0, supplier_cost=10.0, cost_source=source,
        margin=12.0, margin_pct=0.4, score=0.7, llm_verdict="promising", llm_reason="ok",
    )


def _seed(s, title="Thing", finish=True):
    conn = db.connect(s.db_path)
    run = db.start_run(conn)
    db.save_candidates(conn, run, [_cand("A1", title), _cand("A2", source="quoted")])
    db.record_stage(conn, run, StageResult(stage="research", status="degraded", attempts=1,
                                           error_type="KeepaError", message="partial"),
                    "2026-11-11T10:00:00", "2026-11-11T10:00:05")
    db.record_dropped(conn, run, [("D1", "dropped thing", "research", "rank too high")])
    for asin, status in (("A1", "pending"), ("A2", "rejected")):
        db.insert_proposal(conn, Proposal(
            id=None, run_id=run, asin=asin, qty=2, unit_cost=12.0, total=24.0,
            risk_score=20, narrative="fine", status=status))
    if finish:
        db.finish_run(conn, run, "degraded")
    conn.close()


@pytest.fixture
def client(tmp_path):
    s = _settings(tmp_path)
    _seed(s)
    return TestClient(create_app(s))


PAGES = ["/", "/orders", "/orders?status=approved_manual", "/orders?status=rejected",
         "/runs", "/runs/1", "/runs/1?reason=rank", "/products", "/products?q=thing",
         "/settings"]


def test_every_page_renders(client):
    for path in PAGES:
        assert client.get(path).status_code == 200, path
    home = client.get("/").text
    assert r".venv\Scripts\python -m dropship approve 1" in home
    assert r".venv\Scripts\python -m dropship reject 1" in home
    assert "estimated cost" in home
    assert "Pending orders" in home and "Approved today" in home
    assert "$0.00 of $1000" in home and "0 of 100 free" in home
    assert "Claude tokens today" in home and "KeepaError" in home
    assert "cost-quoted" in client.get("/orders?status=rejected").text
    assert "https://www.amazon.com/dp/A1" in client.get("/products").text
    assert "rank too high" in client.get("/runs/1").text
    assert "Edit" in client.get("/settings").text


def test_no_forms_and_only_get_routes(client):
    for path in PAGES:
        assert "<form" not in client.get(path).text.lower()
    app = client.app
    for route in app.routes:
        if getattr(route, "path", "").startswith("/static"):
            continue
        assert set(route.methods) <= {"GET", "HEAD"}, route.path


def test_invalid_order_status_is_404(client):
    assert client.get("/orders?status=bogus").status_code == 404


def test_missing_db_shows_empty_state_and_creates_nothing(tmp_path):
    s = _settings(tmp_path)
    c = TestClient(create_app(s))
    for path in ("/", "/orders", "/runs", "/runs/1", "/products"):
        r = c.get(path)
        assert r.status_code == 200 and EMPTY in r.text, path
    r = c.get("/settings")
    assert r.status_code == 200 and "Data source: canopy" in r.text
    assert not s.db_path.exists()


def test_locked_db_returns_busy_page(client, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(queries, "dashboard", boom)
    r = client.get("/")
    assert r.status_code == 503 and "Database busy" in r.text
    assert 'content="3"' in r.text


def test_other_operational_error_propagates(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    _seed(s)
    def boom(*a, **k):
        raise sqlite3.OperationalError("no such table: x")
    monkeypatch.setattr(queries, "dashboard", boom)
    with pytest.raises(sqlite3.OperationalError):
        TestClient(create_app(s)).get("/")


def test_unknown_run_is_404(client):
    assert client.get("/runs/999").status_code == 404
    assert client.get("/runs/abc").status_code == 404


def test_unknown_path_is_404(client):
    r = client.get("/nope")
    assert r.status_code == 404 and "Not found" in r.text


def test_titles_are_escaped(tmp_path):
    s = _settings(tmp_path)
    _seed(s, title="<script>alert(1)</script> & Co")
    c = TestClient(create_app(s))
    for path in ("/products", "/orders", "/", "/runs/1"):
        text = c.get(path).text
        assert "<script>alert" not in text, path
    assert "&lt;script&gt;" in c.get("/products").text


def test_no_secrets_rendered(client, monkeypatch):
    monkeypatch.setenv("CANOPY_API_KEY", "SECRET-XYZ")
    for path in PAGES:
        assert "SECRET-XYZ" not in client.get(path).text


def test_run_in_progress_banner(tmp_path):
    s = _settings(tmp_path)
    _seed(s, finish=False)
    text = TestClient(create_app(s)).get("/").text
    assert "Run #1 in progress" in text


def test_refresh_meta_present(client):
    text = client.get("/").text
    assert 'http-equiv="refresh" content="30"' in text
    assert "last updated" in text and "/static/style.css" in text
    assert client.get("/static/style.css").status_code == 200
