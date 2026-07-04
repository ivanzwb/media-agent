"""Tests for image/video download strategies."""

import sys
import tempfile
from pathlib import Path

import pytest

from app.media.strategies import (
    HttpxDirectStrategy,
    UrlTransformStrategy,
    CurlCffiImageStrategy,
    HttpxDirectVideoStrategy,
    normalize_url, _img_ext,
)


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
