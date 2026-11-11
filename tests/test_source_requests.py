import json
from datetime import date

import httpx
import pytest

from _india import india
from dropship import monitor
from dropship.canopy_client import CanopyClient, CanopyError
from dropship.db import connect, record_source_requests, source_requests_in_month, start_run
from dropship.keepa_client import KeepaClient
from dropship.models import StageResult
from dropship.monitor import run_pipeline

CANOPY_BODY = {"data": {"amazonBestSellers": {"productResults": {"results": [
    {"asin": "B0A", "title": "t", "price": {"value": 30}}]}}}}


def test_canopy_counts_every_request_including_failures():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=CANOPY_BODY)

    c = CanopyClient("K", "US", http=httpx.Client(
        base_url="https://rest.canopyapi.co", transport=httpx.MockTransport(handler)))
    assert c.requests_made == 0
    with pytest.raises(CanopyError):
        c.bestsellers("a")
    assert c.requests_made == 1
    c.bestsellers("a")
    c.bestsellers("a")  # cached: no request
    assert c.requests_made == 2


def test_keepa_counts_chunked_requests():
    def handler(request):
        return httpx.Response(200, json={"products": [], "tokensLeft": 100})

    k = KeepaClient("K", 1, http=httpx.Client(
        base_url="https://api.keepa.com", transport=httpx.MockTransport(handler)))
    assert k.requests_made == 0
    k.products([f"B{i:09d}" for i in range(250)])
    assert k.requests_made == 3


def _ym():
    t = date.today()
    return t.year, t.month


def test_record_and_sum_source_requests(tmp_path):
    conn = connect(tmp_path / "t.db")
    run_id = start_run(conn)
    record_source_requests(conn, run_id, "canopy", 2)
    record_source_requests(conn, run_id, "canopy", 3)
    record_source_requests(conn, run_id, "keepa", 5)
    y, m = _ym()
    assert source_requests_in_month(conn, "canopy", y, m) == 5
    assert source_requests_in_month(conn, "keepa", y, m) == 5
    assert source_requests_in_month(conn, "canopy", y - 1, m) == 0


def test_requests_this_month_ignores_last_month(tmp_path):
    conn = connect(tmp_path / "t.db")
    run_id = start_run(conn)
    record_source_requests(conn, run_id, "canopy", 4)
    record_source_requests(conn, run_id, "canopy", 7)
    y, m = _ym()
    ly, lm = (y - 1, 12) if m == 1 else (y, m - 1)
    conn.execute(
        "UPDATE source_requests SET created_at = ? WHERE count = 7",
        (f"{ly:04d}-{lm:02d}-28T12:00:00",),
    )
    conn.commit()
    assert source_requests_in_month(conn, "canopy", y, m) == 4
    assert source_requests_in_month(conn, "canopy", ly, lm) == 7


def _patch_stages(monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(
        monitor, "run_research",
        lambda keepa, llm, conn, run_id, s: (StageResult(stage="research", status="ok", attempts=1), {}))
    monkeypatch.setattr(
        monitor, "run_risk",
        lambda llm, conn, run_id, products, s: StageResult(stage="risk", status="ok", attempts=1))


def _settings(tmp_path):
    return india(categories=[1], db_path=tmp_path / "t.db", reports_dir=tmp_path / "reports")


def test_run_pipeline_records_request_count(tmp_path, monkeypatch):
    _patch_stages(monkeypatch)
    s = _settings(tmp_path)
    conn = connect(s.db_path)

    class Counting:
        requests_made = 2

    run_id = run_pipeline(conn, s, Counting(), lambda rid: None, sleep=lambda x: None)
    rows = [tuple(r) for r in conn.execute(
        "SELECT run_id, source, count FROM source_requests")]
    assert rows == [(run_id, s.data_source, 2)]

    run_id2 = run_pipeline(conn, s, object(), lambda rid: None, sleep=lambda x: None)
    rows = [tuple(r) for r in conn.execute(
        "SELECT run_id, source, count FROM source_requests WHERE run_id = ?", (run_id2,))]
    assert rows == [(run_id2, s.data_source, 0)]
