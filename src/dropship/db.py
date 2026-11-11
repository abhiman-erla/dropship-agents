from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from dropship.models import Candidate, Proposal, StageResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stage_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    error_type TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    asin TEXT NOT NULL,
    title TEXT NOT NULL,
    sell_price REAL NOT NULL,
    supplier_cost REAL NOT NULL,
    cost_source TEXT NOT NULL,
    margin REAL NOT NULL,
    margin_pct REAL NOT NULL,
    score REAL NOT NULL,
    llm_verdict TEXT NOT NULL,
    llm_reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    asin TEXT NOT NULL,
    qty INTEGER NOT NULL,
    unit_cost REAL NOT NULL,
    total REAL NOT NULL,
    risk_score INTEGER NOT NULL,
    narrative TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dropped (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    asin TEXT NOT NULL,
    title TEXT NOT NULL,
    stage TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    source TEXT NOT NULL,
    count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now().isoformat()


def _today() -> str:
    return date.today().isoformat()


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_at, status) VALUES (?, ?)", (_now(), "running")
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, status = ? WHERE id = ?",
        (_now(), status, run_id),
    )
    conn.commit()


def record_stage(
    conn: sqlite3.Connection,
    run_id: int,
    result: StageResult,
    started_at: str,
    finished_at: str,
) -> None:
    conn.execute(
        "INSERT INTO stage_runs (run_id, stage, status, attempts, error_type, message,"
        " started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, result.stage, result.status, result.attempts, result.error_type,
         result.message, started_at, finished_at),
    )
    conn.commit()


def save_candidates(conn: sqlite3.Connection, run_id: int, cands: list[Candidate]) -> None:
    conn.executemany(
        "INSERT INTO candidates (run_id, asin, title, sell_price, supplier_cost,"
        " cost_source, margin, margin_pct, score, llm_verdict, llm_reason)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (run_id, c.asin, c.title, c.sell_price, c.supplier_cost, c.cost_source,
             c.margin, c.margin_pct, c.score, c.llm_verdict, c.llm_reason)
            for c in cands
        ],
    )
    conn.commit()


def get_candidates(conn: sqlite3.Connection, run_id: int) -> list[Candidate]:
    rows = conn.execute(
        "SELECT asin, title, sell_price, supplier_cost, cost_source, margin, margin_pct,"
        " score, llm_verdict, llm_reason FROM candidates WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()
    return [Candidate(**dict(r)) for r in rows]


def get_candidate(conn: sqlite3.Connection, run_id: int, asin: str) -> Candidate | None:
    row = conn.execute(
        "SELECT asin, title, sell_price, supplier_cost, cost_source, margin, margin_pct,"
        " score, llm_verdict, llm_reason FROM candidates WHERE run_id = ? AND asin = ?"
        " ORDER BY id LIMIT 1",
        (run_id, asin),
    ).fetchone()
    return Candidate(**dict(row)) if row else None


_PROPOSAL_COLS = "id, run_id, asin, qty, unit_cost, total, risk_score, narrative, status"


def insert_proposal(conn: sqlite3.Connection, p: Proposal) -> int:
    cur = conn.execute(
        "INSERT INTO proposals (run_id, asin, qty, unit_cost, total, risk_score,"
        " narrative, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (p.run_id, p.asin, p.qty, p.unit_cost, p.total, p.risk_score, p.narrative,
         p.status, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_proposal(conn: sqlite3.Connection, pid: int) -> Proposal | None:
    row = conn.execute(
        f"SELECT {_PROPOSAL_COLS} FROM proposals WHERE id = ?", (pid,)
    ).fetchone()
    return Proposal(**dict(row)) if row else None


def list_proposals(conn: sqlite3.Connection, status: str | None = None) -> list[Proposal]:
    if status is None:
        rows = conn.execute(f"SELECT {_PROPOSAL_COLS} FROM proposals ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_PROPOSAL_COLS} FROM proposals WHERE status = ? ORDER BY id",
            (status,),
        ).fetchall()
    return [Proposal(**dict(r)) for r in rows]


def has_pending_proposal(conn: sqlite3.Connection, asin: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM proposals WHERE asin = ? AND status = 'pending' LIMIT 1", (asin,)
    ).fetchone()
    return row is not None


def recently_decided(conn: sqlite3.Connection, asin: str, days: int) -> bool:
    if days <= 0:
        return False
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    row = conn.execute(
        "SELECT 1 FROM proposals WHERE asin = ? AND status IN ('approved_manual', 'rejected')"
        " AND decided_at >= ? LIMIT 1",
        (asin, cutoff),
    ).fetchone()
    return row is not None


def record_dropped(
    conn: sqlite3.Connection, run_id: int, rows: list[tuple[str, str, str, str]]
) -> None:
    conn.executemany(
        "INSERT INTO dropped (run_id, asin, title, stage, reason) VALUES (?, ?, ?, ?, ?)",
        [(run_id, asin, title, stage, reason) for asin, title, stage, reason in rows],
    )
    conn.commit()


def get_dropped(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT asin, title, stage, reason FROM dropped WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def set_proposal_status(conn: sqlite3.Connection, pid: int, status: str) -> None:
    conn.execute(
        "UPDATE proposals SET status = ?, decided_at = ? WHERE id = ?",
        (status, _now(), pid),
    )
    conn.commit()


def approved_total_today(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(total), 0) FROM proposals"
        " WHERE status = 'approved_manual' AND substr(decided_at, 1, 10) = ?",
        (_today(),),
    ).fetchone()
    return float(row[0])


def record_llm_usage(
    conn: sqlite3.Connection, run_id: int, model: str, input_tokens: int, output_tokens: int
) -> None:
    conn.execute(
        "INSERT INTO llm_usage (run_id, model, input_tokens, output_tokens, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (run_id, model, input_tokens, output_tokens, _now()),
    )
    conn.commit()


def llm_tokens_today(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM llm_usage"
        " WHERE substr(created_at, 1, 10) = ?",
        (_today(),),
    ).fetchone()
    return int(row[0])


def last_success(conn: sqlite3.Connection, stage: str) -> datetime | None:
    row = conn.execute(
        "SELECT finished_at FROM stage_runs WHERE stage = ? AND status IN ('ok', 'degraded')"
        " ORDER BY finished_at DESC LIMIT 1",
        (stage,),
    ).fetchone()
    return datetime.fromisoformat(row[0]) if row else None


def get_run(conn: sqlite3.Connection, run_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, started_at, finished_at, status FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    return dict(row) if row else None


def get_stage_runs(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT stage, status, attempts, error_type, message, started_at, finished_at"
        " FROM stage_runs WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def latest_run_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(id) FROM runs").fetchone()
    return int(row[0]) if row and row[0] is not None else None


def record_source_requests(conn: sqlite3.Connection, run_id: int, source: str, count: int) -> None:
    conn.execute(
        "INSERT INTO source_requests (run_id, source, count, created_at) VALUES (?, ?, ?, ?)",
        (run_id, source, count, _now()),
    )
    conn.commit()


def source_requests_in_month(conn: sqlite3.Connection, source: str, year: int, month: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(count), 0) FROM source_requests"
        " WHERE source = ? AND created_at LIKE ? || '%'",
        (source, f"{year:04d}-{month:02d}"),
    ).fetchone()
    return int(row[0])
