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
    has_video INTEGER,
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
    origin TEXT NOT NULL DEFAULT 'rewrite',
    FOREIGN KEY(article_id) REFERENCES articles(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS draft_articles (
    draft_id INTEGER NOT NULL,
    article_id INTEGER NOT NULL,
    rank INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL DEFAULT 'source',
    PRIMARY KEY (draft_id, article_id),
    FOREIGN KEY(draft_id) REFERENCES drafts(id) ON DELETE CASCADE,
    FOREIGN KEY(article_id) REFERENCES articles(id) ON DELETE CASCADE
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
CREATE TABLE IF NOT EXISTS series (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    topic TEXT NOT NULL,
    lang TEXT NOT NULL DEFAULT 'zh',
    depth TEXT NOT NULL DEFAULT 'intermediate',
    parts INTEGER NOT NULL DEFAULT 5,
    style_id TEXT,
    ref_count INTEGER NOT NULL DEFAULT 5,
    engines TEXT NOT NULL DEFAULT '[]',
    knowledge_map TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS series_chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id INTEGER NOT NULL,
    chapter_order INTEGER NOT NULL,
    title TEXT NOT NULL,
    scope TEXT,
    search_queries TEXT NOT NULL DEFAULT '[]',
    prerequisites TEXT NOT NULL DEFAULT '[]',
    preflight_hits INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    draft_id INTEGER,
    summary TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(series_id) REFERENCES series(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_series_chapters_series
    ON series_chapters(series_id, chapter_order);
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
    # Migrate: identify drafts generated from search-driven multi-source runs.
    try:
        conn.execute(
            "ALTER TABLE drafts ADD COLUMN origin TEXT NOT NULL DEFAULT 'rewrite'")
    except sqlite3.OperationalError:
        pass  # column already exists
    # Migrate: series created before the knowledge map existed.
    try:
        conn.execute("ALTER TABLE series ADD COLUMN knowledge_map TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    # Migrate: search settings a single chapter needs to reproduce on a rerun.
    try:
        conn.execute(
            "ALTER TABLE series ADD COLUMN ref_count INTEGER NOT NULL DEFAULT 5")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        conn.execute(
            "ALTER TABLE series ADD COLUMN engines TEXT NOT NULL DEFAULT '[]'")
    except sqlite3.OperationalError:
        pass  # column already exists
    # Migrate: chapters created before the outline could be reviewed.
    try:
        conn.execute(
            "ALTER TABLE series_chapters ADD COLUMN preflight_hits INTEGER")
    except sqlite3.OperationalError:
        pass  # column already exists
    # Migrate: link drafts that belong to a knowledge series. Ordering and
    # lookups run in SQL, so these two live in the table rather than the
    # markdown front-matter.
    try:
        conn.execute("ALTER TABLE drafts ADD COLUMN series_id INTEGER")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE drafts ADD COLUMN series_order INTEGER")
    except sqlite3.OperationalError:
        pass  # column already exists
    # Migrate: cache whether an archived article contains video. Keep existing
    # rows NULL so the Store can lazily inspect their front-matter once.
    article_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(articles)").fetchall()
    }
    if "has_video" not in article_columns:
        conn.execute("ALTER TABLE articles ADD COLUMN has_video INTEGER")
    conn.execute("""CREATE TABLE IF NOT EXISTS draft_articles (
        draft_id INTEGER NOT NULL,
        article_id INTEGER NOT NULL,
        rank INTEGER NOT NULL DEFAULT 0,
        role TEXT NOT NULL DEFAULT 'source',
        PRIMARY KEY (draft_id, article_id),
        FOREIGN KEY(draft_id) REFERENCES drafts(id) ON DELETE CASCADE,
        FOREIGN KEY(article_id) REFERENCES articles(id) ON DELETE CASCADE
    )""")
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
    # Migrate: set default huggingface_mirror if not configured
    row = conn.execute(
        "SELECT value FROM settings WHERE key='huggingface_mirror'"
    ).fetchone()
    if not row or not (row[0] or "").strip():
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("huggingface_mirror", "https://hf-mirror.com"))
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


def update_op(conn: sqlite3.Connection, op_id: int, *,
              stats: dict | None = None, target_id: int | None = None) -> None:
    assignments: list[str] = []
    params: list[object] = []
    if stats is not None:
        assignments.append("stats_json=?")
        params.append(_json.dumps(stats, ensure_ascii=False))
    if target_id is not None:
        assignments.append("target_id=?")
        params.append(target_id)
    if not assignments:
        return
    params.append(op_id)
    conn.execute(
        f"UPDATE operations SET {', '.join(assignments)} WHERE id=?", params)
    conn.commit()


def cancel_op(conn: sqlite3.Connection, op_id: int) -> None:
    now = _dt.now(_tz.utc).isoformat()
    conn.execute(
        "UPDATE operations SET status='cancelled', finished_at=?, "
        "error='cancelled' WHERE id=?",
        (now, op_id),
    )
    conn.commit()


def get_latest_op(conn: sqlite3.Connection, op_type: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM operations WHERE op_type=? ORDER BY id DESC LIMIT 1",
        (op_type,),
    ).fetchone()
    return _op_row_to_dict(row) if row else None


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
