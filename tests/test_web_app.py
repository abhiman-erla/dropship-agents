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


def _set_run_start(s, run_id=1, started="2026-11-11T12:34:56.789012"):
    conn = db.connect(s.db_path)
    conn.execute("UPDATE runs SET started_at = ? WHERE id = ?", (started, run_id))
    conn.commit()
    conn.close()


def test_when_filter():
    from dropship.web.app import when
    assert when("2026-11-11T12:34:56.789012") == "Nov 11, 12:34"
    assert when("2026-01-05T09:07:00") == "Jan 05, 09:07"
    assert when(None) == "—"
    assert when("") == "—"
    assert when("not a time") == "not a time"


def test_timestamps_are_readable(tmp_path):
    s = _settings(tmp_path)
    _seed(s)
    _set_run_start(s)
    conn = db.connect(s.db_path)
    conn.execute("UPDATE proposals SET created_at = '2026-11-11T12:40:00',"
                 " decided_at = '2026-11-11T13:05:00' WHERE asin = 'A2'")
    conn.commit()
    conn.close()
    c = TestClient(create_app(s))
    for path in ("/", "/runs", "/runs/1"):
        text = c.get(path).text
        assert "Nov 11, 12:34" in text, path
        assert "2026-11-11T12:34" not in text, path
    rejected = c.get("/orders?status=rejected").text
    assert "created Nov 11, 12:40 · decided Nov 11, 13:05" in rejected


def test_products_last_seen_shows_run_and_date(tmp_path):
    s = _settings(tmp_path)
    _seed(s)
    _set_run_start(s)
    assert "#1 · Nov 11, 12:34" in TestClient(create_app(s)).get("/products").text


def test_requests_hint_before_any_counted_request(client):
    assert "counting starts with your next run" in client.get("/").text


def test_requests_hint_counted_since(tmp_path):
    s = _settings(tmp_path)
    _seed(s)
    conn = db.connect(s.db_path)
    db.record_source_requests(conn, 1, "canopy", 4)
    conn.execute("UPDATE source_requests SET created_at = '2026-11-02T08:15:00'")
    conn.commit()
    conn.close()
    text = TestClient(create_app(s)).get("/").text
    assert "counted since Nov 02, 08:15" in text
    assert "counting starts with your next run" not in text


@pytest.mark.parametrize("digits", [23, 5000])
def test_oversized_run_id_is_404(client, digits):
    r = client.get("/runs/" + "1" * digits)
    assert r.status_code == 404 and "Not found" in r.text


def test_settings_freshness_note(client):
    assert ("Edit config.toml / supplier_quotes.csv to change these, then restart dropship"
            " web (quotes refresh automatically).") in client.get("/settings").text


def test_dropped_asins_link_to_amazon(client):
    assert 'href="https://www.amazon.com/dp/D1"' in client.get("/runs/1").text


def test_templates_build_no_amazon_urls_inline():
    from pathlib import Path
    from dropship.web import app as app_module
    for tpl in (Path(app_module.__file__).parent / "templates").glob("*.html"):
        assert "amazon.com" not in tpl.read_text(encoding="utf-8"), tpl.name


def test_busy_page_topbar_matches_its_refresh(client, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(queries, "dashboard", boom)
    text = client.get("/").text
    assert "auto-refresh 30s" not in text and "auto-refresh 3s" in text
    assert "auto-refresh 30s" in client.get("/runs").text


def test_reason_filter_links(tmp_path):
    s = _settings(tmp_path)
    conn = db.connect(s.db_path)
    run = db.start_run(conn)
    db.record_dropped(conn, run, [
        ("D1", "t", "research", "blocked brand: Acme"),
        ("D2", "t", "research", "blocked brand: Zed"),
        ("D3", "t", "research", "blocked brand: Acme"),
        ("D4", "t", "research", "missing price"),
    ])
    conn.close()
    c = TestClient(create_app(s))
    text = c.get(f"/runs/{run}").text
    assert 'href="?reason=blocked%20brand"' in text
    assert "blocked brand (3)" in text and "missing price (1)" in text
    assert text.index("blocked brand (3)") < text.index("missing price (1)")
    assert f'href="/runs/{run}"' in text and ">all</a>" in text
    filtered = c.get(f"/runs/{run}?reason=missing%20price").text
    assert "D4" in filtered and "D1" not in filtered
    assert "<form" not in filtered.lower()


def test_tables_are_wrapped_and_css_prevents_overflow(client):
    for path in PAGES:
        text = client.get(path).text
        assert text.count("<table") == text.count('<div class="table-wrap"><table'), path
    css = client.get("/static/style.css").text
    assert "overflow-wrap:anywhere" in css and ".table-wrap { overflow-x:auto" in css
    assert "white-space:pre-wrap; word-break:break-all" in css
