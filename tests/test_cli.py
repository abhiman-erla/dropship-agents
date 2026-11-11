from pathlib import Path

import pytest

from dropship import cli, db
from dropship.config import Settings
from dropship.models import Proposal


KEEPA_CFG = 'data_source = "keepa"\n'


def _write_cfg(tmp_path: Path, extra: str = "") -> Path:
    cfg = tmp_path / "config.toml"
    cats = "[1]" if KEEPA_CFG in extra else '["bestsellers_kitchen"]'
    cfg.write_text(f"categories = {cats}\nmax_order_value = 5000.0\ndaily_spend_cap = 15000.0\n{extra}", encoding="utf-8")
    return cfg


def _seed(tmp_path: Path, total: float = 1234.5) -> None:
    conn = db.connect(tmp_path / "data" / "dropship.db")
    run_id = db.start_run(conn)
    db.insert_proposal(conn, Proposal(
        id=None, run_id=run_id, asin="B0TESTASIN", qty=3, unit_cost=total / 3,
        total=total, risk_score=22, narrative="n", status="pending"))
    conn.close()


def test_resolve_paths_relative_to_config_dir(tmp_path):
    s = cli.resolve_paths(Settings(categories=["x"]), tmp_path / "cfgdir" / "config.toml")
    base = tmp_path / "cfgdir"
    assert s.db_path == base / "data" / "dropship.db"
    assert s.reports_dir == base / "reports"
    assert s.supplier_quotes_path == base / "supplier_quotes.csv"


def test_resolve_paths_keeps_absolute(tmp_path):
    abs_db = tmp_path / "abs.db"
    s = cli.resolve_paths(Settings(categories=["x"], db_path=abs_db), tmp_path / "config.toml")
    assert s.db_path == abs_db


def test_missing_config_exits_2(tmp_path, capsys):
    assert cli.main(["--config", str(tmp_path / "nope.toml"), "proposals"]) == 2
    assert capsys.readouterr().err


def test_invalid_config_exits_2(tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text("categories = []\n", encoding="utf-8")
    assert cli.main(["--config", str(cfg), "proposals"]) == 2
    assert "categories" in capsys.readouterr().err


def test_missing_keepa_key_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("KEEPA_API_KEY", raising=False)
    cfg = _write_cfg(tmp_path, KEEPA_CFG)
    assert cli.main(["--config", str(cfg), "run"]) == 2
    assert "KEEPA_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "data").exists()


def test_cli_missing_canopy_key_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CANOPY_API_KEY", raising=False)
    cfg = _write_cfg(tmp_path)
    assert cli.main(["--config", str(cfg), "run"]) == 2
    assert "CANOPY_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "data").exists()


def test_cli_builds_keepa_client(tmp_path, monkeypatch):
    monkeypatch.setenv("KEEPA_API_KEY", "k")
    sentinel = object()
    monkeypatch.setattr(cli, "KeepaClient", lambda key, domain: sentinel)
    got = {}

    def fake_pipeline(conn, s, keepa, llm_factory):
        got["client"] = keepa
        run_id = db.start_run(conn)
        db.finish_run(conn, run_id, "ok")
        return run_id

    monkeypatch.setattr(cli, "run_pipeline", fake_pipeline)
    cfg = _write_cfg(tmp_path, KEEPA_CFG)
    assert cli.main(["--config", str(cfg), "run"]) == 0
    assert got["client"] is sentinel


def test_cli_builds_canopy_client(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPY_API_KEY", "ck")
    made = {}

    def fake_canopy(key, domain):
        made["args"] = (key, domain)
        return object()

    monkeypatch.setattr(cli, "CanopyClient", fake_canopy)

    def fake_pipeline(conn, s, keepa, llm_factory):
        run_id = db.start_run(conn)
        db.finish_run(conn, run_id, "ok")
        return run_id

    monkeypatch.setattr(cli, "run_pipeline", fake_pipeline)
    assert cli.main(["--config", str(_write_cfg(tmp_path)), "run"]) == 0
    assert made["args"] == ("ck", "US")


def test_approve_flow(tmp_path, capsys):
    _seed(tmp_path)
    cfg = _write_cfg(tmp_path)
    assert cli.main(["--config", str(cfg), "approve", "1"]) == 0
    assert "manual-1" in capsys.readouterr().out
    assert cli.main(["--config", str(cfg), "approve", "1"]) == 1
    assert "not pending" in capsys.readouterr().err


def test_reject_flow(tmp_path, capsys):
    _seed(tmp_path)
    cfg = _write_cfg(tmp_path)
    assert cli.main(["--config", str(cfg), "reject", "1"]) == 0
    assert cli.main(["--config", str(cfg), "reject", "1"]) == 1
    assert "not pending" in capsys.readouterr().err


def test_proposals_lists_pending(tmp_path, capsys):
    _seed(tmp_path)
    cfg = _write_cfg(tmp_path)
    assert cli.main(["--config", str(cfg), "proposals"]) == 0
    out = capsys.readouterr().out
    assert "B0TESTASIN" in out and "1234.50" in out and "#1" in out


def test_proposals_status_filter_and_empty(tmp_path, capsys):
    _seed(tmp_path)
    cfg = _write_cfg(tmp_path)
    assert cli.main(["--config", str(cfg), "proposals", "--status", "rejected"]) == 0
    assert "No proposals." in capsys.readouterr().out


def test_report_without_runs_exits_1(tmp_path, capsys):
    cfg = _write_cfg(tmp_path)
    assert cli.main(["--config", str(cfg), "report"]) == 1
    assert "No runs yet." in capsys.readouterr().out


def test_report_writes_latest(tmp_path, capsys):
    _seed(tmp_path)
    cfg = _write_cfg(tmp_path)
    assert cli.main(["--config", str(cfg), "report"]) == 0
    path = Path(capsys.readouterr().out.strip())
    assert path.exists() and path.parent == tmp_path / "reports"


def test_run_prints_summary(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KEEPA_API_KEY", "k")
    monkeypatch.setattr(cli, "KeepaClient", lambda key, domain: object())

    def fake_pipeline(conn, s, keepa, llm_factory):
        run_id = db.start_run(conn)
        db.finish_run(conn, run_id, "ok")
        return run_id

    monkeypatch.setattr(cli, "run_pipeline", fake_pipeline)
    cfg = _write_cfg(tmp_path, KEEPA_CFG)
    assert cli.main(["--config", str(cfg), "run"]) == 0
    out = capsys.readouterr().out
    assert "ok" in out and "reports" in out


def test_run_failed_status_exits_1(tmp_path, monkeypatch):
    monkeypatch.setenv("KEEPA_API_KEY", "k")
    monkeypatch.setattr(cli, "KeepaClient", lambda key, domain: object())

    def fake_pipeline(conn, s, keepa, llm_factory):
        run_id = db.start_run(conn)
        db.finish_run(conn, run_id, "failed")
        return run_id

    monkeypatch.setattr(cli, "run_pipeline", fake_pipeline)
    assert cli.main(["--config", str(_write_cfg(tmp_path, KEEPA_CFG)), "run"]) == 1


def test_llm_factory_returns_none_when_anthropic_unavailable(tmp_path, monkeypatch):
    import anthropic

    def boom():
        raise anthropic.AnthropicError("no key")

    monkeypatch.setattr(cli.anthropic, "Anthropic", boom)
    conn = db.connect(tmp_path / "t.db")
    factory = cli.make_llm_factory(conn, Settings(categories=["x"]))
    assert factory(1) is None


def test_python_dash_m_dropship_help():
    import subprocess
    import sys

    r = subprocess.run([sys.executable, "-m", "dropship", "--help"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert "approve" in r.stdout


@pytest.mark.parametrize("extra", ["max_offer_count = 0\n", "typo_key = 1\n", "price_min = [\n"])
def test_bad_config_exits_2_without_traceback(tmp_path, capsys, extra):
    assert cli.main(["--config", str(_write_cfg(tmp_path, extra)), "proposals"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("config error:") and "Traceback" not in err


_CRED_ENV = [
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_CONFIG_DIR",
    "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID", "XDG_CONFIG_HOME",
]


def _isolate_credentials(tmp_path, monkeypatch):
    for name in _CRED_ENV:
        monkeypatch.delenv(name, raising=False)
    for name in ("HOME", "USERPROFILE", "APPDATA"):
        monkeypatch.setenv(name, str(tmp_path))


def test_llm_factory_returns_none_without_credentials(tmp_path, monkeypatch):
    # anthropic.Anthropic() does not raise without credentials; it fails at request time.
    _isolate_credentials(tmp_path, monkeypatch)
    conn = db.connect(tmp_path / "t.db")
    assert cli.make_llm_factory(conn, Settings(categories=["x"]))(1) is None


def test_llm_factory_returns_llm_with_api_key(tmp_path, monkeypatch):
    _isolate_credentials(tmp_path, monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    conn = db.connect(tmp_path / "t.db")
    assert cli.make_llm_factory(conn, Settings(categories=["x"]))(1) is not None


def _seed_with_candidate(tmp_path, cost_source="quoted", candidate=True):
    from dropship.models import Candidate

    conn = db.connect(tmp_path / "data" / "dropship.db")
    run_id = db.start_run(conn)
    if candidate:
        db.save_candidates(conn, run_id, [Candidate(
            asin="B0TESTASIN", title="Steel water bottle", sell_price=999.0,
            supplier_cost=411.5, cost_source=cost_source, margin=300.0, margin_pct=0.3,
            score=1.0)])
    db.insert_proposal(conn, Proposal(
        id=None, run_id=run_id, asin="B0TESTASIN", qty=3, unit_cost=411.5, total=1234.5,
        risk_score=22, narrative="Low competition; try three.", status="pending"))
    conn.close()


def test_proposals_shows_title_cost_and_narrative(tmp_path, capsys):
    _seed_with_candidate(tmp_path)
    assert cli.main(["--config", str(_write_cfg(tmp_path)), "proposals"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "#1  B0TESTASIN  qty=3  total=1234.50  risk=22  pending"
    assert lines[1] == "    Steel water bottle  unit=411.50 (quoted)"
    assert lines[2] == "    Low competition; try three."


def test_proposals_flags_estimated_cost(tmp_path, capsys):
    _seed_with_candidate(tmp_path, cost_source="estimated")
    assert cli.main(["--config", str(_write_cfg(tmp_path)), "proposals"]) == 0
    assert "unit=411.50 (estimated cost)" in capsys.readouterr().out


def test_proposals_without_candidate_shows_unknown(tmp_path, capsys):
    _seed_with_candidate(tmp_path, candidate=False)
    assert cli.main(["--config", str(_write_cfg(tmp_path)), "proposals"]) == 0
    assert capsys.readouterr().out.splitlines()[1] == "    ?  unit=411.50 (unknown)"


def test_main_silences_httpx_info_logging(tmp_path):
    import logging

    logging.getLogger("httpx").setLevel(logging.NOTSET)
    cli.main(["--config", str(_write_cfg(tmp_path)), "proposals"])
    assert logging.getLogger("httpx").level == logging.WARNING


def test_web_bad_config_exits_2(tmp_path, capsys):
    assert cli.main(["--config", str(tmp_path / "nope.toml"), "web"]) == 2
    assert "config error" in capsys.readouterr().err


def test_web_port_in_use_exits_1(tmp_path, capsys):
    import socket
    cfg = _write_cfg(tmp_path)
    holder = socket.socket()
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            holder.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        assert cli.main(["--config", str(cfg), "web", "--port", str(port)]) == 1
    finally:
        holder.close()
    assert "is busy" in capsys.readouterr().err


def test_web_serves_on_localhost_only(tmp_path, capsys):
    import socket
    from fastapi import FastAPI
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    calls = []
    s = cli.resolve_paths(Settings(categories=["x"]), tmp_path / "config.toml")
    assert cli._cmd_web(s, port, serve=lambda app, host, p: calls.append((app, host, p))) == 0
    assert len(calls) == 1
    app, host, p = calls[0]
    assert (host, p) == ("127.0.0.1", port)
    assert isinstance(app, FastAPI)
    assert f"Dashboard: http://localhost:{port}" in capsys.readouterr().out


def test_web_does_not_create_db(tmp_path, monkeypatch):
    import socket
    cfg = _write_cfg(tmp_path)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(cli, "_serve", lambda app, host, p: None)
    assert cli.main(["--config", str(cfg), "web", "--port", str(port)]) == 0
    assert not (tmp_path / "data" / "dropship.db").exists()


def _old_db(tmp_path: Path) -> Path:
    import sqlite3
    path = tmp_path / "data" / "dropship.db"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(db._SCHEMA.split("CREATE TABLE IF NOT EXISTS dropped")[0])
    conn.execute("INSERT INTO runs (started_at, status) VALUES ('2026-01-01T00:00:00', 'success')")
    conn.commit()
    conn.close()
    return path


def _free_port() -> int:
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_web_upgrades_old_db_schema(tmp_path, monkeypatch):
    import sqlite3
    path = _old_db(tmp_path)
    cfg = _write_cfg(tmp_path)
    monkeypatch.setattr(cli, "_serve", lambda app, host, p: None)
    assert cli.main(["--config", str(cfg), "web", "--port", str(_free_port())]) == 0
    conn = sqlite3.connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    runs = conn.execute("SELECT started_at, status FROM runs").fetchall()
    conn.close()
    assert {"source_requests", "dropped"} <= tables
    assert runs == [("2026-01-01T00:00:00", "success")]


def test_dashboard_renders_on_upgraded_old_db(tmp_path):
    from fastapi.testclient import TestClient
    _old_db(tmp_path)
    s = cli.resolve_paths(Settings(categories=["x"]), tmp_path / "config.toml")
    apps = []
    assert cli._cmd_web(s, _free_port(), serve=lambda app, h, p: apps.append(app)) == 0
    assert TestClient(apps[0]).get("/").status_code == 200
