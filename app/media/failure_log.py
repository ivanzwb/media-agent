"""Failures / skipped URLs JSONL log with rotation.

Writes one JSON line per failed or skipped media download to
``data/download_failures.log`` so users have a concrete list of URLs to
inspect or retry.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.Lock()
_MAX_ENTRIES = 10_000

_LOG_FILE = "download_failures.log"


def _data_dir() -> Path:
    """Resolve the configured data directory (singleton)."""
    raw = os.environ.get("MEDIA_AGENT_DATA_DIR", "data")
    return Path(raw).resolve()


def _log_path() -> Path:
    return _data_dir() / _LOG_FILE


def log_failure(
    url: str,
    source_url: str | None = None,
    media_type: str = "unknown",
    error: str = "",
    downloader: str | None = None,
    skipped: bool = False,
) -> None:
    """Append one JSONL entry to the download failure log.

    Thread-safe (mutex).  Rotates to ``_MAX_ENTRIES`` on every write.
    """
    entry: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "url": url,
        "source_url": source_url or "",
        "media_type": media_type,
        "error": error,
        "downloader": downloader or "",
        "skipped": skipped,
    }

    with _lock:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            return  # best-effort: don't crash the pipeline on a logging glitch

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
