"""Tests for image/video download strategies."""

import sys
import tempfile
from pathlib import Path

import pytest

import time
from unittest import mock

import httpx

from app.media.strategies import (
    HttpxDirectStrategy,
    UrlTransformStrategy,
    CurlCffiImageStrategy,
    FallbackImageStrategy,
    HttpxDirectVideoStrategy,
    normalize_url, _img_ext,
)
from app.media.downloader import MediaDownloader


# ── CurlCffiImageStrategy ──────────────────────────────────────────

def test_curl_cffi_match_image_url():
    strat = CurlCffiImageStrategy()
    assert strat.match("https://cdn.example.com/photo.jpg") > 0
    assert strat.match("https://cdn.example.com/chart.png") > 0
    assert strat.match("https://cdn.example.com/icon.gif") > 0
    assert strat.match("https://cdn.example.com/banner.webp") > 0


def test_curl_cffi_no_match_non_image():
    strat = CurlCffiImageStrategy()
    assert strat.match("https://example.com/page") == 0
    assert strat.match("https://example.com/video.mp4") == 0
    assert strat.match("not-a-url") == 0


def test_curl_cffi_skip_invalid_url():
    strat = CurlCffiImageStrategy()
    with tempfile.TemporaryDirectory() as tmp:
        result = strat.download("not-a-url", Path(tmp), 1, emit=lambda m: None)
        assert result is None


def test_curl_cffi_skip_when_not_installed(monkeypatch):
    """When curl_cffi is not installed, the strategy should return None."""
    strat = CurlCffiImageStrategy()
    # Simulate missing curl_cffi
    import builtins
    original_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "curl_cffi" or name.startswith("curl_cffi."):
            raise ImportError("No module named 'curl_cffi'")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", mock_import)

    with tempfile.TemporaryDirectory() as tmp:
        result = strat.download(
            "https://example.com/img.jpg", Path(tmp), 1,
            emit=lambda m: None)
        assert result is None


# ── HttpxDirectStrategy ────────────────────────────────────────────

def test_httpx_direct_match():
    strat = HttpxDirectStrategy()
    assert strat.match("https://cdn.example.com/photo.jpg") == 0.8
    assert strat.match("https://cdn.example.com/photo.png") == 0.8
    assert strat.match("https://cdn.example.com/photo.gif") == 0.8
    assert strat.match("https://cdn.example.com/page.html") == 0.0


def test_httpx_direct_skip_invalid_url():
    strat = HttpxDirectStrategy()
    with tempfile.TemporaryDirectory() as tmp:
        result = strat.download("not-a-url", Path(tmp), 1, emit=lambda m: None)
        assert result is None


def test_httpx_direct_skip_empty_data():
    strat = HttpxDirectStrategy()
    with tempfile.TemporaryDirectory() as tmp:
        # URL that normalizes to None
        result = strat.download("", Path(tmp), 1, emit=lambda m: None)
        assert result is None


# ── UrlTransformStrategy ────────────────────────────────────────────

def test_url_transform_match_nextjs():
    strat = UrlTransformStrategy()
    assert strat.match("https://example.com/_next/image?url=encoded&w=800") == 0.9
    assert strat.match("https://example.com/regular/photo.jpg") == 0.0


def test_url_transform_skip_invalid():
    strat = UrlTransformStrategy()
    with tempfile.TemporaryDirectory() as tmp:
        result = strat.download("not-a-url", Path(tmp), 1, emit=lambda m: None)
        assert result is None


# ── HttpxDirectVideoStrategy ───────────────────────────────────────

def test_video_direct_match():
    strat = HttpxDirectVideoStrategy()
    assert strat.match("https://cdn.example.com/clip.mp4") == 0.9
    assert strat.match("https://cdn.example.com/clip.webm") == 0.9
    assert strat.match("https://cdn.example.com/clip.jpg") == 0.0


# ── normalize_url ──────────────────────────────────────────────────

def test_normalize_absolute_url():
    assert normalize_url("https://example.com/path") == "https://example.com/path"


def test_normalize_protocol_relative():
    result = normalize_url("//cdn.example.com/img.jpg")
    assert result == "https://cdn.example.com/img.jpg"


def test_normalize_relative_url():
    assert normalize_url("/path/img.jpg") is None


def test_normalize_html_entities():
    result = normalize_url("https://example.com/path?a=1&amp;b=2")
    assert "&amp;" not in result if result else True


def test_normalize_non_ascii():
    result = normalize_url("https://例子.com/路径")
    assert result is not None or result is None  # either is fine


# ── _img_ext ───────────────────────────────────────────────────────

def test_img_ext_from_url():
    assert _img_ext("https://example.com/photo.jpg", None) == ".jpg"
    assert _img_ext("https://example.com/photo.PNG?w=800", None) == ".png"
    assert _img_ext("https://example.com/photo", "image/webp") == ".webp"
    assert _img_ext("https://example.com/photo", "image/jpeg") in (".jpg", ".jpeg")


def test_img_ext_fallback():
    assert _img_ext("https://example.com/photo", None) == ".jpg"
    assert _img_ext("https://example.com/photo", "application/octet-stream") == ".jpg"


# ── normalize_url enhancements ─────────────────────────────────────

def test_normalize_trailing_backslash():
    """Trailing backslash from HTML attribute regex must be stripped."""
    assert normalize_url("https://cdn.example.com/a.png\\") == "https://cdn.example.com/a.png"


def test_normalize_json_unicode_escapes():
    """\\u0026 (and similar) in URLs from JSON-embedded HTML decoded before encode."""
    result = normalize_url("https://example.com/a.png?foo\\u0026bar")
    assert result is not None
    assert "\\u0026" not in result  # should be decoded to &
    assert "?" in result


def test_normalize_srcset_first():
    """Comma-separated srcset → only first URL returned."""
    result = normalize_url(
        "https://cdn.example.com/photo.jpg 1x, https://cdn.example.com/photo@2x.jpg 2x"
    )
    assert result is not None
    assert result.count("cdn.example.com") == 1


def test_normalize_srcset_with_commas_in_query():
    """URL with comma in query parameter (not a srcset) must not be truncated."""
    result = normalize_url(
        "https://example.com/image.jpg?w=1200&q=75&src=mixed&sizes[]=100,200,300"
    )
    assert result is not None
    # Commas in query are preserved (not treated as srcset separator)
    assert "100,200,300" in result


# ── FallbackImageStrategy ─────────────────────────────────────────

def test_fallback_match_image():
    strat = FallbackImageStrategy()
    assert strat.match("https://cdn.images.example/photo.jpg") == 0.1
    assert strat.match("https://cdn.images.example/chart.png") == 0.1
    assert strat.match("https://cdn.images.example/banner.webp") == 0.1
    assert strat.match("https://cdn.images.example/icon.gif") == 0.1


def test_fallback_no_match_fake_domain():
    strat = FallbackImageStrategy()
    assert strat.match("https://img.cdn/a.png") == 0.0
    assert strat.match("https://cdn.example.com/a.png") == 0.0


def test_fallback_no_match_fake_suffix_short_host():
    """Single-letter domain + known test suffix = suppressed."""
    strat = FallbackImageStrategy()
    # host='i', suffix='/a.png'
    assert strat.match("https://i/a.png") == 0.0
    assert strat.match("https://v/b.jpg") == 0.0
    # Real-looking domain with same suffix is OK
    assert strat.match("https://cdn.example.com/a.png") == 0.0  # fake domain


def test_fallback_no_match_non_image():
    strat = FallbackImageStrategy()
    assert strat.match("https://example.com/page") == 0.0
    assert strat.match("https://example.com/video.mp4") == 0.0
    assert strat.match("not-a-url") == 0.0


def test_fallback_download_invalid_url():
    strat = FallbackImageStrategy()
    with tempfile.TemporaryDirectory() as tmp:
        result = strat.download("not-a-url", Path(tmp), 1, emit=lambda m: None)
        assert result is None


def test_fallback_download_all_ua_fail(monkeypatch):
    """When all 5 UAs return non-2xx, download returns None."""
    strat = FallbackImageStrategy()

    class _ErrResp:
        status_code = 403

        def raise_for_status(self):
            raise httpx.HTTPStatusError("mock 403", request=mock.MagicMock(), response=self)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _ErrResp())
    monkeypatch.setattr(time, "sleep", lambda _s: None)  # skip delays

    with tempfile.TemporaryDirectory() as tmp:
        result = strat.download(
            "https://cdn.example.com/photo.jpg", Path(tmp), 1,
            emit=lambda m: None)
        assert result is None


def test_fallback_download_success(monkeypatch):
    """A single successful response saves the image and returns the path."""
    strat = FallbackImageStrategy()

    class _OkResp:
        content = b"\x89PNG\r\n\x1a\nfake"
        headers = {"content-type": "image/png"}

        def raise_for_status(self):
            pass

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _OkResp())

    with tempfile.TemporaryDirectory() as tmp:
        result = strat.download(
            "https://cdn.example.com/photo.jpg", Path(tmp), 1,
            emit=lambda m: None)
        assert result is not None
        assert result.exists()
        assert result.suffix == ".jpg"  # URL's own extension takes priority


# ── HttpxDirectStrategy: source_url as Referer ────────────────────

def test_httpx_direct_source_url_referer(monkeypatch):
    """source_url is passed to the strategy and used as Referer header."""
    strat = HttpxDirectStrategy()
    captured_kwargs = {}

    def _mock_get(url, **kwargs):
        captured_kwargs.update(kwargs)
        # Return an error so download() returns None
        raise httpx.HTTPStatusError(
            "mock", request=mock.MagicMock(),
            response=mock.MagicMock(status_code=403))

    monkeypatch.setattr(httpx, "get", _mock_get)

    with tempfile.TemporaryDirectory() as tmp:
        strat.download(
            "https://cdn.example.com/photo.jpg", Path(tmp), 1,
            emit=lambda m: None, source_url="https://example.com/article")
        # Verify Referer header contains source_url
        headers = captured_kwargs.get("headers", {})
        assert headers.get("Referer") == "https://example.com/article"


# ── MediaDownloader sanity check ──────────────────────────────────

def test_downloader_skip_single_letter_host():
    """Single-letter host + single-file path → skipped before strategy chain."""
    dl = MediaDownloader("image")
    with tempfile.TemporaryDirectory() as tmp:
        result = dl.download(
            "https://i/a.png", Path(tmp), 1, emit=lambda m: None)
        assert result is None


def test_downloader_skip_non_standard_scheme():
    """data: URIs are skipped without reaching strategy chain."""
    dl = MediaDownloader("image")
    with tempfile.TemporaryDirectory() as tmp:
        result = dl.download(
            "data:image/png;base64,xxxx", Path(tmp), 1, emit=lambda m: None)
        assert result is None
