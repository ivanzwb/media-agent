from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    url TEXT NOT NULL,
    mode TEXT,
    topics TEXT,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    source_name TEXT,
    source_type TEXT,
    topic TEXT,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    archive_path TEXT,
    fingerprint TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'archived'
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL,
    draft_path TEXT,
    cover_image TEXT,
    title_cn TEXT,
    status TEXT NOT NULL DEFAULT 'drafted',
    updated_at TEXT NOT NULL,
    FOREIGN KEY(article_id) REFERENCES articles(id)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats_json TEXT,
    status TEXT NOT NULL DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    op_type TEXT NOT NULL,
    target_id INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    logs TEXT DEFAULT '[]',
    error TEXT,
    stats_json TEXT DEFAULT '{}'
);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Migrate: add title_cn column if missing
    try:
        conn.execute("ALTER TABLE drafts ADD COLUMN title_cn TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    # Migrate: drop platform column (no longer needed)
    try:
        conn.execute("ALTER TABLE drafts DROP COLUMN platform")
    except sqlite3.OperationalError:
        pass  # column may not exist (SQLite < 3.35 or already dropped)
    # Migrate: add score column
    try:
        conn.execute("ALTER TABLE drafts ADD COLUMN score REAL")
    except sqlite3.OperationalError:
        pass  # column already exists
    # Migrate: add operations table (for state that survives page refreshes)
    conn.execute("""CREATE TABLE IF NOT EXISTS operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        op_type TEXT NOT NULL,
        target_id INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'running',
        started_at TEXT NOT NULL,
        finished_at TEXT,
        logs TEXT DEFAULT '[]',
        error TEXT,
        stats_json TEXT DEFAULT '{}'
    )""")
    # Migrate: set default github_mirror if not configured
    row = conn.execute(
        "SELECT value FROM settings WHERE key='github_mirror'"
    ).fetchone()
    if not row or not (row[0] or "").strip():
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("github_mirror", "https://ghproxy.net/"))
    conn.commit()


# ── Operations table helpers (persistent state across page refreshes) ──────

import json as _json
from datetime import datetime as _dt, timezone as _tz


def start_op(conn: sqlite3.Connection, op_type: str, target_id: int = 0) -> int:
    """Create a new operation record and return its ID."""
    now = _dt.now(_tz.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO operations (op_type, target_id, started_at) VALUES (?, ?, ?)",
        (op_type, target_id, now),
    )
    conn.commit()
    return cur.lastrowid


def _op_log(conn: sqlite3.Connection, op_id: int, line: str) -> None:
    """Append a log line to an operation."""
    row = conn.execute(
        "SELECT logs FROM operations WHERE id=?", (op_id,)
    ).fetchone()
    if not row:
        return
    logs: list[str] = _json.loads(row[0] or "[]")
    logs.append(line)
    if len(logs) > 500:
        logs = logs[-500:]
    conn.execute(
        "UPDATE operations SET logs=? WHERE id=?",
        (_json.dumps(logs, ensure_ascii=False), op_id),
    )
    # Don't commit here — caller batches commits


def finish_op(conn: sqlite3.Connection, op_id: int,
              stats: dict | None = None) -> None:
    """Mark an operation as completed."""
    now = _dt.now(_tz.utc).isoformat()
    conn.execute(
        "UPDATE operations SET status='done', finished_at=?, stats_json=? WHERE id=?",
        (now, _json.dumps(stats or {}, ensure_ascii=False), op_id),
    )
    conn.commit()


def fail_op(conn: sqlite3.Connection, op_id: int, error: str) -> None:
    """Mark an operation as failed."""
    now = _dt.now(_tz.utc).isoformat()
    conn.execute(
        "UPDATE operations SET status='error', finished_at=?, error=? WHERE id=?",
        (now, error, op_id),
    )
    conn.commit()


def get_running_ops(conn: sqlite3.Connection) -> list[dict]:
    """Return all currently running operations."""
    rows = conn.execute(
        "SELECT * FROM operations WHERE status='running' ORDER BY started_at"
    ).fetchall()
    return [_op_row_to_dict(r) for r in rows]


def recover_stale_ops(conn: sqlite3.Connection, max_age_seconds: int = 3600) -> int:
    """Mark stale running operations as failed on server startup.

    Operations that have been 'running' for longer than *max_age_seconds*
    (default 1 hour) are considered stale — the server must have crashed or
    restarted while they were in progress.  Returns the number of operations
    recovered.
    """
    from datetime import timedelta as _td
    cutoff = (_dt.now(_tz.utc) - _td(seconds=max_age_seconds)).isoformat()
    cur = conn.execute(
        "UPDATE operations SET status='error', finished_at=?, "
        "error='服务器重启时自动标记为失败' "
        "WHERE status='running' AND started_at < ?",
        (_dt.now(_tz.utc).isoformat(), cutoff),
    )
    conn.commit()
    return cur.rowcount


def get_op(conn: sqlite3.Connection, op_id: int) -> dict | None:
    """Return a single operation by ID."""
    row = conn.execute(
        "SELECT * FROM operations WHERE id=?", (op_id,)
    ).fetchone()
    return _op_row_to_dict(row) if row else None


def _op_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "type": row["op_type"],
        "target_id": row["target_id"],
        "status": row["status"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "logs": _json.loads(row["logs"] or "[]"),
        "error": row["error"],
        "stats": _json.loads(row["stats_json"] or "{}"),
    }
