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
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

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
        emit: Callable = _noop, **kwargs,
    ) -> Path | None:
        """Try strategies sorted by confidence, fall through on failure."""
        scored = [(s, s.match(url)) for s in self._strategies]
        scored.sort(key=lambda x: -x[1])

        for strategy, confidence in scored:
            if confidence <= 0:
                continue
            try:
                result = strategy.download(url, dest, idx, emit, **kwargs)
                if result:
                    return result
                emit(f"    [{strategy.name}] 不适用，尝试下一策略")
            except Exception as e:
                emit(f"    [{strategy.name}] 异常：{e}")

        emit(f"    所有下载策略均失败（{self.media_type}）：{(url or '')[:80]}")
        logger.warning("All %s strategies failed for: %s", self.media_type, url)
        return None
