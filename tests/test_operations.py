"""Tests for the operations DB persistence layer."""

import sqlite3
import tempfile
from pathlib import Path

from app.db import init_db, start_op, finish_op, fail_op, _op_log, get_running_ops, get_op


def _tmp_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    return conn


def test_start_op_creates_record():
    conn = _tmp_db()
    op_id = start_op(conn, "rewrite", 42)
    assert op_id > 0

    row = conn.execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
    assert row["op_type"] == "rewrite"
    assert row["target_id"] == 42
    assert row["status"] == "running"
    assert row["started_at"] is not None


def test_op_log_appends():
    conn = _tmp_db()
    op_id = start_op(conn, "rewrite", 1)
    _op_log(conn, op_id, "line 1")
    _op_log(conn, op_id, "line 2")
    conn.commit()

    op = get_op(conn, op_id)
    assert op["logs"] == ["line 1", "line 2"]


def test_op_log_truncates_old():
    conn = _tmp_db()
    op_id = start_op(conn, "rewrite", 1)
    for i in range(600):
        _op_log(conn, op_id, f"line {i}")
    conn.commit()

    op = get_op(conn, op_id)
    assert len(op["logs"]) == 500
    assert op["logs"][0] == "line 100"  # oldest kept
    assert op["logs"][-1] == "line 599"  # newest


def test_op_log_non_existent():
    conn = _tmp_db()
    _op_log(conn, 99999, "ghost")  # should not raise
    conn.commit()


def test_finish_op():
    conn = _tmp_db()
    op_id = start_op(conn, "rewrite", 1)
    finish_op(conn, op_id, {"images": 5})

    op = get_op(conn, op_id)
    assert op["status"] == "done"
    assert op["finished_at"] is not None
    assert op["stats"] == {"images": 5}


def test_fail_op():
    conn = _tmp_db()
    op_id = start_op(conn, "rewrite", 1)
    fail_op(conn, op_id, "LLM timeout")

    op = get_op(conn, op_id)
    assert op["status"] == "error"
    assert op["error"] == "LLM timeout"
    assert op["finished_at"] is not None


def test_get_running_ops_filters_by_status():
    conn = _tmp_db()
    id1 = start_op(conn, "rewrite", 1)
    id2 = start_op(conn, "narration", 5)
    id3 = start_op(conn, "rewrite", 3)

    finish_op(conn, id2)  # done, should not appear

    running = get_running_ops(conn)
    assert len(running) == 2
    ids = {op["id"] for op in running}
    assert id1 in ids
    assert id3 in ids
    assert id2 not in ids  # finished


def test_get_running_ops_empty():
    conn = _tmp_db()
    assert get_running_ops(conn) == []


def test_get_op_returns_none_for_missing():
    conn = _tmp_db()
    assert get_op(conn, 99999) is None


def test_multiple_ops_different_types():
    conn = _tmp_db()
    types = ["rewrite", "narration", "resynth", "localize", "video", "run"]
    ids = {}
    for t in types:
        ids[t] = start_op(conn, t, 1)

    running = get_running_ops(conn)
    assert len(running) == len(types)

    for t in types:
        finish_op(conn, ids[t])

    assert get_running_ops(conn) == []
