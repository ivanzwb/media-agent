"""Source fetch failures JSONL log with rotation.

Writes one JSON line per failed source fetch to
``data/fetch_failures.log`` so users and admins have a concrete list of
sources that failed, why, and at what URL — for retry or investigation.

Fields per entry:
  - timestamp (ISO-8601 UTC)
  - source_name
  - source_type ("rss" | "scrape")
  - url
  - error_type (exception class name)
  - status_code (int, 0 if N/A)
  - error_message
  - response_snippet (first 200 chars of response text, if any)
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

_lock = threading.Lock()
_MAX_ENTRIES = 10_000

_LOG_FILE = "fetch_failures.log"


def _data_dir() -> Path:
    raw = os.environ.get("MEDIA_AGENT_DATA_DIR", "data")
    return Path(raw).resolve()


def _log_path() -> Path:
    return _data_dir() / _LOG_FILE


def _extract_details(exc: Exception) -> dict[str, Any]:
    """Extract structured details from a fetch exception."""
    details: dict[str, Any] = {
        "error_type": type(exc).__name__,
        "status_code": 0,
        "error_message": str(exc) or type(exc).__name__,
        "response_snippet": "",
    }
    if isinstance(exc, httpx.HTTPStatusError):
        details["status_code"] = exc.response.status_code
        try:
            text = exc.response.text or ""
        except Exception:  # noqa: BLE001
            text = ""
        details["response_snippet"] = text.strip()[:200]
        if not details["error_message"]:
            details["error_message"] = f"HTTP {exc.response.status_code}"
    elif isinstance(exc, httpx.TimeoutException):
        details["error_message"] = f"Timeout after configured timeout"
    elif isinstance(exc, httpx.ConnectError):
        details["error_message"] = "Connection refused or unreachable"
    return details


def log_fetch_failure(
    source_name: str,
    source_type: str,
    url: str,
    exception: Exception | None = None,
    error_type: str = "",
    error_message: str = "",
    status_code: int = 0,
    response_snippet: str = "",
) -> None:
    """Append one JSONL entry to the fetch failure log.

    Thread-safe (mutex).  Rotates to ``_MAX_ENTRIES`` on every write.
    """
    out_error_type = error_type
    out_status_code = status_code
    out_error_message = error_message
    out_response_snippet = response_snippet

    if exception is not None:
        details = _extract_details(exception)
        out_error_type = details["error_type"]
        out_status_code = details["status_code"]
        out_error_message = details["error_message"]
        out_response_snippet = details["response_snippet"]

    entry: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source_name": source_name,
        "source_type": source_type,
        "url": url,
        "error_type": out_error_type,
        "status_code": out_status_code,
        "error_message": out_error_message,
        "response_snippet": out_response_snippet,
    }

    with _lock:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            return  # best-effort

        _rotate(path)


def _rotate(path: Path) -> None:
    """Keep only the last ``_MAX_ENTRIES`` lines (truncate in-place)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= _MAX_ENTRIES:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines[-_MAX_ENTRIES:])
    except OSError:
        pass
