"""Pluggable media download pipeline.

Public API:
    MediaDownloader        — strategy-based orchestrator
    HttpxDirectStrategy    — image direct HTTP (default)
    UrlTransformStrategy   — decode proxy URLs (_next/image, …)
    HttpxDirectVideoStrategy — video direct HTTP
    YtDlpStrategy          — platform embeds (YouTube, Bilibili, …)
    make_image_downloader  — ready-to-use image downloader
    make_video_downloader  — ready-to-use video downloader
"""

from app.media.downloader import MediaDownloader
from app.media.strategies import (
    HttpxDirectStrategy,
    HttpxDirectVideoStrategy,
    UrlTransformStrategy,
    YtDlpStrategy,
)


def make_image_downloader() -> MediaDownloader:
    """Return a pre-configured MediaDownloader for images."""
    d = MediaDownloader("image")
    d.add_strategy(HttpxDirectStrategy())
    d.add_strategy(UrlTransformStrategy())
    return d


def make_video_downloader() -> MediaDownloader:
    """Return a pre-configured MediaDownloader for videos."""
    d = MediaDownloader("video")
    d.add_strategy(HttpxDirectVideoStrategy())
    d.add_strategy(YtDlpStrategy())
    return d
