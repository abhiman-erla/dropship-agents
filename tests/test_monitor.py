import json
from datetime import datetime

import httpx
import pytest

from dropship import monitor
from dropship.config import Settings
from dropship.db import connect, insert_proposal, record_stage, save_candidates, start_run
from dropship.keepa_client import KeepaError, KeepaOutOfTokens
from dropship.models import Candidate, Proposal, StageResult
from dropship.monitor import maybe_alert, run_pipeline, run_stage, write_report


class Sleeper:
    def __init__(self):
        self.calls = []

    def __call__(self, secs):
        self.calls.append(secs)


def _ok():
    return StageResult(stage="research", status="ok", attempts=1)


def test_retry_then_success():
    n = {"i": 0}

    def fn():
        n["i"] += 1
        if n["i"] <= 2:
            raise httpx.ConnectError("down")
        return _ok()

    sl = Sleeper()
    r = run_stage("research", fn, sl)
    assert r.status == "ok" and r.attempts == 3 and r.stage == "research"
    assert sl.calls == [2, 4]


def test_retry_exhausted_fails():
    def fn():
        raise httpx.ConnectError("down")

    sl = Sleeper()
    r = run_stage("research", fn, sl)
    assert r.status == "failed" and r.attempts == 3
    assert r.error_type == "ConnectError"
    assert sl.calls == [2, 4]


def test_degraded_result_kept():
    r = run_stage(
        "research",
        lambda: StageResult(stage="research", status="degraded", attempts=1, error_type="E"),
        Sleeper(),
    )
    assert r.status == "degraded" and r.attempts == 1 and r.error_type == "E"


def test_keepa_out_of_tokens_waits_once():
    n = {"i": 0}

    def fn():
        n["i"] += 1
        if n["i"] == 1:
            raise KeepaOutOfTokens("empty", refill_in_ms=2_000_000)
        return _ok()

    sl = Sleeper()
    r = run_stage("research", fn, sl)
    assert r.status == "ok" and sl.calls == [900] and r.attempts == 2


def test_keepa_out_of_tokens_twice_fails():
    def fn():
        raise KeepaOutOfTokens("empty", refill_in_ms=5_000)

    sl = Sleeper()
    r = run_stage("research", fn, sl)
    assert r.status == "failed" and r.error_type == "KeepaOutOfTokens"
    assert sl.calls == [5.0] and r.attempts == 2


def test_keepa_5xx_is_transient():
    n = {"i": 0}

    def fn():
        n["i"] += 1
        if n["i"] == 1:
            raise KeepaError("bad gateway", status_code=502)
        return _ok()

    sl = Sleeper()
    assert run_stage("research", fn, sl).status == "ok"
    assert sl.calls == [2]


def test_unknown_error_not_retried():
    def fn():
        raise ValueError("nope")

    sl = Sleeper()
    r = run_stage("research", fn, sl)
    assert r.status == "failed" and r.attempts == 1
    assert r.error_type == "ValueError" and r.message == "nope"
    assert sl.calls == []


class BoomKeepa:
    def bestsellers(self, category):
        raise ValueError("bad data")


def _settings(tmp_path):
    return Settings(
        categories=[1], db_path=tmp_path / "t.db", reports_dir=tmp_path / "reports"
    )


def test_pipeline_skips_risk_after_research_failure(tmp_path, monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    s = _settings(tmp_path)
    conn = connect(s.db_path)
    run_id = run_pipeline(conn, s, BoomKeepa(), lambda rid: None, sleep=Sleeper())
    stages = {
        r["stage"]: r["status"]
        for r in conn.execute("SELECT stage, status FROM stage_runs WHERE run_id = ?", (run_id,))
    }
    assert stages == {"research": "failed", "risk": "skipped"}
    status = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
    assert status == "failed"
    assert list((tmp_path / "reports").glob(f"*-run-{run_id}.md"))


def test_pipeline_degraded_status(tmp_path, monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    s = _settings(tmp_path)
    conn = connect(s.db_path)

    def fake_research(keepa, llm, conn, run_id, s):
        return StageResult(stage="research", status="degraded", attempts=1), {}

    def fake_risk(llm, conn, run_id, products, s):
        return StageResult(stage="risk", status="ok", attempts=1)

    monkeypatch.setattr(monitor, "run_research", fake_research)
    monkeypatch.setattr(monitor, "run_risk", fake_risk)
    run_id = run_pipeline(conn, s, object(), lambda rid: None, sleep=Sleeper())
    status = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
    assert status == "degraded"


def _seed_run(conn):
    run_id = start_run(conn)
    record_stage(
        conn, run_id, StageResult(stage="research", status="ok", attempts=1),
        "2026-01-01T00:00:00", "2026-01-01T00:01:00",
    )
    save_candidates(conn, run_id, [Candidate(
        asin="B01", title="Item", sell_price=2000.0, supplier_cost=1200.0,
        cost_source="quoted", margin=800.0, margin_pct=0.4, score=0.9,
    )])
    insert_proposal(conn, Proposal(
        id=None, run_id=run_id, asin="B01", qty=1, unit_cost=1200.0, total=1200.0,
        risk_score=10, narrative="n", status="pending",
    ))
    return run_id


SECTIONS = [
    "## Summary", "## Stages", "## Top candidates",
    "## Proposals awaiting approval", "## Errors", "## LLM tokens today",
]


def test_report_contains_sections_and_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("KEEPA_API_KEY", "SECRETKEY")
    s = _settings(tmp_path)
    conn = connect(s.db_path)
    run_id = _seed_run(conn)
    path = write_report(conn, run_id, s)
    text = path.read_text(encoding="utf-8")
    for h in SECTIONS:
        assert h in text
    assert "SECRETKEY" not in text
    assert "dropship approve" in text and "B01" in text
    assert path.name.endswith(f"-run-{run_id}.md")


def test_stale_warning(tmp_path):
    s = _settings(tmp_path)
    conn = connect(s.db_path)
    run_id = start_run(conn)
    text = write_report(conn, run_id, s).read_text(encoding="utf-8")
    assert "**STALE:**" in text


def test_no_stale_warning_when_recent(tmp_path):
    s = _settings(tmp_path)
    conn = connect(s.db_path)
    run_id = _seed_run(conn)
    conn.execute("UPDATE stage_runs SET finished_at = ?", (datetime.now().isoformat(),))
    conn.commit()
    assert "STALE" not in write_report(conn, run_id, s).read_text(encoding="utf-8")


def _client(status, posts):
    def handler(request):
        posts.append(request)
        return httpx.Response(status)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_alert_only_on_bad_status(tmp_path):
    rp = tmp_path / "r.md"
    rp.write_text("report", encoding="utf-8")
    posts = []
    assert maybe_alert("ok", rp, "http://hook", _client(200, posts)) is False
    assert posts == []
    assert maybe_alert("degraded", rp, None, _client(200, posts)) is False
    assert posts == []
    stages = [
        {"stage": "research", "status": "degraded", "attempts": 1, "error_type": "NoLLM",
         "message": "no LLM client configured", "started_at": "a", "finished_at": "b"},
        {"stage": "risk", "status": "ok", "attempts": 1, "error_type": "", "message": "",
         "started_at": "c", "finished_at": "d"},
    ]
    assert maybe_alert("degraded", rp, "http://hook", _client(200, posts), stages=stages) is True
    assert len(posts) == 1
    body = json.loads(posts[0].content)
    assert body["status"] == "degraded" and body["summary"] == "report"
    assert body["stages"] == [
        {"stage": "research", "status": "degraded", "error_type": "NoLLM",
         "message": "no LLM client configured"},
        {"stage": "risk", "status": "ok", "error_type": "", "message": ""},
    ]
    assert maybe_alert("failed", rp, "http://hook", _client(500, [])) is False


def test_alert_swallows_http_error_without_leaking_url(tmp_path, caplog):
    rp = tmp_path / "r.md"
    rp.write_text("report", encoding="utf-8")

    def handler(request):
        raise httpx.ConnectError("down")

    c = httpx.Client(transport=httpx.MockTransport(handler))
    with caplog.at_level("WARNING"):
        assert maybe_alert("failed", rp, "http://secret-hook/abc", c) is False
    assert "secret-hook" not in caplog.text


def test_report_proposals_show_title_cost_and_narrative(tmp_path):
    s = _settings(tmp_path)
    conn = connect(s.db_path)
    run_id = _seed_run(conn)
    save_candidates(conn, run_id, [Candidate(
        asin="B02", title="Guess-priced lamp", sell_price=1000.0, supplier_cost=350.0,
        cost_source="estimated", margin=400.0, margin_pct=0.4, score=0.5,
    )])
    insert_proposal(conn, Proposal(
        id=None, run_id=run_id, asin="B02", qty=2, unit_cost=350.0, total=700.0,
        risk_score=30, narrative="Volatile price.", status="pending",
    ))
    insert_proposal(conn, Proposal(
        id=None, run_id=run_id, asin="B03", qty=1, unit_cost=50.0, total=50.0,
        risk_score=5, narrative="orphan", status="pending",
    ))
    text = write_report(conn, run_id, s).read_text(encoding="utf-8")
    section = text.split("## Proposals awaiting approval")[1].split("## Errors")[0]
    assert "Item" in section and "unit 1200.00 (quoted)" in section
    assert "Guess-priced lamp" in section and "unit 350.00 (estimated cost)" in section
    assert "Volatile price." in section
    assert "? - unit 50.00 (unknown)" in section and "orphan" in section


def test_alert_malformed_url_returns_false_and_closes_default_client(
    tmp_path, monkeypatch, caplog
):
    rp = tmp_path / "r.md"
    rp.write_text("report", encoding="utf-8")
    made = []
    real_client = httpx.Client

    def fake_client(**kw):
        c = real_client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        made.append(c)
        return c

    monkeypatch.setattr(monitor.httpx, "Client", fake_client)
    with caplog.at_level("WARNING"):
        # e.g. a trailing newline pasted into ALERT_WEBHOOK_URL
        assert maybe_alert("failed", rp, "http://hook/secret\n") is False
    assert "InvalidURL" in caplog.text and "secret" not in caplog.text
    assert len(made) == 1 and made[0].is_closed


def test_alert_unsupported_protocol_returns_false(tmp_path):
    rp = tmp_path / "r.md"
    rp.write_text("report", encoding="utf-8")

    def handler(request):
        raise httpx.UnsupportedProtocol("no ftp")

    c = httpx.Client(transport=httpx.MockTransport(handler))
    assert maybe_alert("failed", rp, "ftp://hook", c) is False


def test_pipeline_alert_carries_stages(tmp_path, monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://hook")
    seen = {}

    def fake_alert(status, path, url, http=None, stages=None):
        seen.update(status=status, stages=stages)
        return True

    monkeypatch.setattr(monitor, "maybe_alert", fake_alert)
    s = _settings(tmp_path)
    run_pipeline(connect(s.db_path), s, BoomKeepa(), lambda rid: None, sleep=Sleeper())
    assert seen["status"] == "failed"
    assert [(st["stage"], st["status"]) for st in seen["stages"]] == [
        ("research", "failed"), ("risk", "skipped")]
    assert seen["stages"][0]["error_type"] == "ValueError"


@pytest.mark.parametrize("exc", [
    httpx.ReadError("reset"), httpx.WriteError("broken pipe"),
    httpx.RemoteProtocolError("peer closed"), httpx.ReadTimeout("slow"),
])
def test_network_errors_are_transient(exc):
    n = {"i": 0}

    def fn():
        n["i"] += 1
        if n["i"] == 1:
            raise exc
        return _ok()

    sl = Sleeper()
    assert run_stage("research", fn, sl).status == "ok" and sl.calls == [2]
