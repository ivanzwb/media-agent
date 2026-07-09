"""Unit tests for app.sources.failure_log — fetch failure JSONL logging."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from app.sources.failure_log import (
    _data_dir,
    _extract_details,
    _log_path,
    _rotate,
    _MAX_ENTRIES,
    log_fetch_failure,
)


# ── _data_dir & _log_path ──────────────────────────────────────────────────


def test_data_dir_default():
    """Without MEDIA_AGENT_DATA_DIR, defaults to ``data``."""
    with patch.dict(os.environ, {}, clear=True):
        d = _data_dir()
        assert d.name == "data"


def test_data_dir_from_env():
    """Uses MEDIA_AGENT_DATA_DIR when set."""
    with patch.dict(os.environ, {"MEDIA_AGENT_DATA_DIR": "/custom/path"}):
        d = _data_dir()
        assert d == Path("/custom/path").resolve()


def test_log_path_uses_data_dir():
    """_log_path() returns data_dir / fetch_failures.log."""
    with patch("app.sources.failure_log._data_dir") as mock_dir:
        mock_dir.return_value = Path("/base")
        assert _log_path() == Path("/base") / "fetch_failures.log"


# ── _extract_details ──────────────────────────────────────────────────────


def test_extract_details_http_status_error():
    """HTTPStatusError yields status_code and response_snippet."""
    request = httpx.Request("GET", "https://example.com/feed")
    response = httpx.Response(
        403, request=request,
        text="<html><title>Access Denied</title><body>Cloudflare</body></html>",
    )
    exc = httpx.HTTPStatusError("Forbidden", request=request, response=response)
    details = _extract_details(exc)
    assert details["error_type"] == "HTTPStatusError"
    assert details["status_code"] == 403
    assert details["error_message"] == "Forbidden"
    assert "Cloudflare" in details["response_snippet"]


def test_extract_details_http_status_error_default_message():
    """HTTPStatusError uses str() of the exception, falling back to class name."""
    request = httpx.Request("GET", "https://example.com/feed")
    response = httpx.Response(404, request=request)
    # httpx.RedirectRequest is a factory, so use the actual exception directly
    exc = httpx.HTTPStatusError("Not Found", request=request, response=response)
    details = _extract_details(exc)
    assert details["error_message"] == "Not Found"
    assert details["status_code"] == 404


def test_extract_details_http_status_error_text_exception():
    """When response.text raises, snippet stays empty."""

    class FragileResponse:
        status_code = 502
        @property
        def text(self):
            raise RuntimeError("broken")

    request = httpx.Request("GET", "https://example.com/feed")
    response = FragileResponse()
    exc = httpx.HTTPStatusError("Bad Gateway", request=request, response=response)  # type: ignore[arg-type]
    details = _extract_details(exc)
    assert details["status_code"] == 502
    assert details["response_snippet"] == ""


def test_extract_details_timeout():
    """TimeoutException gets a descriptive message."""
    exc = httpx.TimeoutException("Read timed out")
    details = _extract_details(exc)
    assert details["error_type"] == "TimeoutException"
    assert details["status_code"] == 0
    assert "Timeout" in details["error_message"]


def test_extract_details_connect_error():
    """ConnectError gets a descriptive message."""
    exc = httpx.ConnectError("ConnectError: [Errno 111] Connection refused")
    details = _extract_details(exc)
    assert details["error_type"] == "ConnectError"
    assert details["error_message"] == "Connection refused or unreachable"


def test_extract_details_generic_exception():
    """Generic exception falls back to class name for message."""
    exc = ValueError("something broke")
    details = _extract_details(exc)
    assert details["error_type"] == "ValueError"
    assert details["error_message"] == "something broke"
    assert details["status_code"] == 0
    assert details["response_snippet"] == ""


def test_extract_details_exception_with_empty_str():
    """Exception with empty str() uses class name."""
    exc = RuntimeError()
    details = _extract_details(exc)
    assert details["error_message"] == "RuntimeError"


# ── log_fetch_failure (no exception) ──────────────────────────────────────


def test_log_fetch_failure_explicit_params(tmp_path, monkeypatch):
    """Write entry with explicit error_type/error_message, no exception."""
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    log_fetch_failure(
        source_name="TestSource",
        source_type="scrape",
        url="https://example.com/page",
        error_type="ParseError",
        error_message="Invalid HTML structure",
        status_code=200,
    )
    logpath = tmp_path / "fetch_failures.log"
    assert logpath.exists()
    lines = logpath.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["source_name"] == "TestSource"
    assert entry["source_type"] == "scrape"
    assert entry["error_type"] == "ParseError"
    assert entry["error_message"] == "Invalid HTML structure"
    assert entry["status_code"] == 200
    assert entry["response_snippet"] == ""


# ── log_fetch_failure (with exception) ────────────────────────────────────


def test_log_fetch_failure_with_httpstatuserror(tmp_path, monkeypatch):
    """Write entry derived from an HTTPStatusError exception."""
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    request = httpx.Request("GET", "https://example.com/feed")
    response = httpx.Response(403, request=request, text="blocked by WAF")
    exc = httpx.HTTPStatusError("Forbidden", request=request, response=response)

    log_fetch_failure(
        source_name="TSMC",
        source_type="rss",
        url="https://example.com/feed",
        exception=exc,
    )
    entry = json.loads(
        (tmp_path / "fetch_failures.log").read_text(encoding="utf-8")
    )
    assert entry["source_name"] == "TSMC"
    assert entry["error_type"] == "HTTPStatusError"
    assert entry["status_code"] == 403
    assert "WAF" in entry["response_snippet"]


def test_log_fetch_failure_with_timeout(tmp_path, monkeypatch):
    """Write entry derived from a TimeoutException."""
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    exc = httpx.TimeoutException("Connect timeout")
    log_fetch_failure(
        source_name="SlowSource",
        source_type="rss",
        url="https://slow.example.com/feed",
        exception=exc,
    )
    entry = json.loads(
        (tmp_path / "fetch_failures.log").read_text(encoding="utf-8")
    )
    assert entry["error_type"] == "TimeoutException"
    assert "Timeout" in entry["error_message"]


def test_log_fetch_failure_with_connect_error(tmp_path, monkeypatch):
    """Write entry derived from a ConnectError."""
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    exc = httpx.ConnectError("Connection refused")
    log_fetch_failure(
        source_name="Unreachable",
        source_type="rss",
        url="https://unreachable.example.com/feed",
        exception=exc,
    )
    entry = json.loads(
        (tmp_path / "fetch_failures.log").read_text(encoding="utf-8")
    )
    assert entry["error_type"] == "ConnectError"
    assert "unreachable" in entry["error_message"].lower()


def test_log_fetch_failure_exception_overrides_explicit(tmp_path, monkeypatch):
    """When both exception and explicit params are given, exception takes precedence."""
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    exc = httpx.TimeoutException("real timeout")
    log_fetch_failure(
        source_name="OverrideTest",
        source_type="scrape",
        url="https://example.com",
        exception=exc,
        error_type="FakeError",  # should be ignored
        error_message="fake msg",
    )
    entry = json.loads(
        (tmp_path / "fetch_failures.log").read_text(encoding="utf-8")
    )
    assert entry["error_type"] == "TimeoutException"
    assert "Timeout" in entry["error_message"]


# ── rotation ──────────────────────────────────────────────────────────────


def test_rotate_below_limit(tmp_path):
    """_rotate does nothing when line count <= _MAX_ENTRIES."""
    logpath = tmp_path / "fetch_failures.log"
    lines = [json.dumps({"i": i}) + "\n" for i in range(5)]
    logpath.write_text("".join(lines), encoding="utf-8")

    _rotate(logpath)
    assert len(logpath.read_text(encoding="utf-8").strip().split("\n")) == 5


def test_rotate_above_limit(tmp_path):
    """_rotate keeps only the last _MAX_ENTRIES lines."""
    logpath = tmp_path / "fetch_failures.log"
    n = _MAX_ENTRIES + 50
    lines = [json.dumps({"i": i}) + "\n" for i in range(n)]
    logpath.write_text("".join(lines), encoding="utf-8")

    _rotate(logpath)
    remaining = logpath.read_text(encoding="utf-8").strip().split("\n")
    assert len(remaining) == _MAX_ENTRIES
    # Verify the last entries are kept
    last_entry = json.loads(remaining[-1])
    assert last_entry["i"] == n - 1


def test_rotate_non_existent_file(tmp_path):
    """_rotate silently handles missing file."""
    logpath = tmp_path / "nonexistent.log"
    _rotate(logpath)  # should not raise


def test_rotate_oserror_on_read(tmp_path):
    """_rotate silently handles OSError on read."""
    logpath = tmp_path / "fetch_failures.log"
    logpath.write_text("line\n" * (_MAX_ENTRIES + 10), encoding="utf-8")
    with patch("builtins.open", side_effect=OSError("read error")):
        _rotate(logpath)  # should not raise


# ── edge cases ────────────────────────────────────────────────────────────


def test_log_fetch_failure_oserror_on_write(tmp_path, monkeypatch):
    """OSError during write is silently swallowed."""
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    logpath = tmp_path / "fetch_failures.log"
    logpath.write_text("existing\n", encoding="utf-8")

    # Mock open for append to raise OSError
    original_open = open  # noqa: SIM115 — need to keep reference
    def _fail_on_append(*args, **kwargs):
        mode = args[1] if len(args) > 1 else kwargs.get("mode", "r")
        if "a" in mode:
            raise OSError("permission denied")
        return original_open(*args, **kwargs)

    with patch("builtins.open", side_effect=_fail_on_append):
        log_fetch_failure(
            source_name="Fail", source_type="rss", url="https://x.com/fail",
            error_type="Err", error_message="msg",
        )
    # File should remain unchanged
    assert logpath.read_text(encoding="utf-8") == "existing\n"


def test_log_fetch_failure_creates_parent_dir(tmp_path, monkeypatch):
    """Parent directory is created if it doesn't exist."""
    sub = tmp_path / "subdir"
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(sub))
    log_fetch_failure(
        source_name="NewDir", source_type="rss", url="https://x.com",
        error_type="Err", error_message="msg",
    )
    assert sub.exists()
    assert (sub / "fetch_failures.log").exists()


def test_log_fetch_failure_appends(tmp_path, monkeypatch):
    """Multiple calls append to the same file."""
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    log_fetch_failure(source_name="A", source_type="rss", url="https://a.com",
                       error_type="T1", error_message="m1")
    log_fetch_failure(source_name="B", source_type="rss", url="https://b.com",
                       error_type="T2", error_message="m2")
    lines = (tmp_path / "fetch_failures.log").read_text(
        encoding="utf-8"
    ).strip().split("\n")
    assert len(lines) == 2


def test_log_fetch_failure_unicode(tmp_path, monkeypatch):
    """Non-ASCII characters in source_name and URL are preserved."""
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    log_fetch_failure(
        source_name="中文来源",
        source_type="rss",
        url="https://例子.测试/feed",
        error_type="TypeError",
        error_message="连接超时",
    )
    entry = json.loads(
        (tmp_path / "fetch_failures.log").read_text(encoding="utf-8")
    )
    assert entry["source_name"] == "中文来源"
    assert "例子" in entry["url"]
    assert entry["error_message"] == "连接超时"


def test_log_fetch_failure_timestamp_iso_format(tmp_path, monkeypatch):
    """Timestamp is valid ISO-8601."""
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    from datetime import datetime
    log_fetch_failure(source_name="TS", source_type="rss", url="https://x.com",
                       error_type="E", error_message="m")
    entry = json.loads(
        (tmp_path / "fetch_failures.log").read_text(encoding="utf-8")
    )
    # Should parse without error
    datetime.fromisoformat(entry["timestamp"])
