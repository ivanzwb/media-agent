"""
Strategy-based media downloader with fallback chain.

Each strategy reports a confidence score for a URL; the chain sorts by confidence
descending, tries each in turn, and falls through on failure or exception.

Usage:
    downloader = MediaDownloader("image")
    downloader.add_strategy(HttpxDirectStrategy())
    downloader.add_strategy(UrlTransformStrategy())

    path = downloader.download(url, dest, idx, emit=print)
    # Returns local Path or None
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from app.media.failure_log import log_failure

logger = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """Base class for a download strategy."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable strategy name (for logging)."""
        ...

    @abstractmethod
    def match(self, url: str) -> float:
        """Return confidence 0.0–1.0 for how well this strategy handles *url*."""
        ...

    @abstractmethod
    def download(
        self, url: str, dest: Path, idx: int,
        emit: Callable = print, **kwargs,
    ) -> Path | None:
        """Download *url* into *dest*, returning the local Path or None."""
        ...


def _noop(*_a, **_k):
    pass


class MediaDownloader:
    """Ordered fallback chain of download strategies for one media type."""

    def __init__(self, media_type: str) -> None:
        self.media_type = media_type
        self._strategies: list[BaseStrategy] = []

    def add_strategy(self, strategy: BaseStrategy) -> None:
        self._strategies.append(strategy)

    def download(
        self, url: str, dest: Path, idx: int,
        emit: Callable = _noop,
        source_url: str | None = None,
        **kwargs,
    ) -> Path | None:
        """Try strategies sorted by confidence, fall through on failure.

        Parameters
        ----------
        source_url
            The article/container URL for the failure log (not used for
            download logic).
        """
        # --- Pre-processing: clean extraction artifacts ---
        clean_url = (url or "").strip().rstrip("\\")
        # Decode \u00xx escapes that survive from JSON-embedded HTML
        clean_url = re.sub(r'\\u00([0-9a-fA-F]{2})',
                           lambda m: chr(int(m.group(1), 16)), clean_url)

        # --- Check for non-downloadable URI schemes before any strategy ---
        lower = clean_url.strip().lower()
        if lower.startswith("data:"):
            emit(f"    [downloader] 跳过 data: URI #{idx}（不下载）")
            log_failure(url, source_url, self.media_type,
                        error="data: URI (skipped)", skipped=True)
            return None
        if lower.startswith("//"):
            log_failure(url, source_url, self.media_type,
                        error="protocol-relative URL (skipped)", skipped=True)
            # Fall through — strategies *may* handle it via normalize_url
        elif not lower.startswith(("http://", "https://", "ftp://")):
            emit(f"    [downloader] 跳过非标准 URI #{idx}：{(url or '')[:60]}")
            log_failure(url, source_url, self.media_type,
                        error="non-standard URI scheme (skipped)", skipped=True)
            return None

        # --- Sanity check: skip obviously fake URLs (test data) ---
        parsed = urlparse(clean_url)
        host = parsed.hostname or ""
        path_lower = parsed.path.lower()
        # Single-letter hostname + single-file path = almost certainly test data
        # e.g. https://i/a.png, https://v/b.mp4
        if len(host) <= 2 and re.match(r'^/[a-z]\.(png|jpg|jpeg|gif|webp|mp4)$', path_lower):
            emit(f"    [downloader] 跳过疑似测试数据 #{idx}：{clean_url[:60]}")
            log_failure(url, source_url, self.media_type,
                        error="suspected test/fake URL (skipped)", skipped=True)
            return None

        # --- Strategy chain ---
        scored = [(s, s.match(url)) for s in self._strategies]
        scored.sort(key=lambda x: -x[1])

        errors: list[tuple[str, str]] = []
        for strategy, confidence in scored:
            if confidence <= 0:
                continue
            try:
                result = strategy.download(url, dest, idx, emit, **kwargs)
                if result:
                    return result
                emit(f"    [{strategy.name}] 不适用，尝试下一策略")
            except Exception as e:
                err_msg = str(e) or e.__class__.__name__
                emit(f"    [{strategy.name}] 异常：{err_msg}")
                errors.append((strategy.name, err_msg))

        # --- All strategies failed — log the failure ---
        error_detail = "; ".join(
            f"[{name}] {msg}" for name, msg in errors
        ) if errors else "all strategies returned None"
        emit(f"    所有下载策略均失败（{self.media_type}）：{(url or '')[:80]}")
        logger.warning("All %s strategies failed for: %s", self.media_type, url)
        log_failure(url, source_url, self.media_type,
                    error=error_detail, downloader=self.media_type)
        return None
