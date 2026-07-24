from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from app.platforms.image_compat import prepare_platform_image


def _cleanup(prepared) -> None:
    if prepared.is_temp:
        prepared.path.unlink(missing_ok=True)


def test_supported_png_is_reused(tmp_path):
    source = tmp_path / "image.png"
    Image.new("RGB", (100, 80), "blue").save(source, "PNG")
    prepared = prepare_platform_image(source, platform="wechat")
    assert prepared.path == source
    assert prepared.is_temp is False


@pytest.mark.parametrize("fmt,suffix", [
    ("WEBP", ".webp"),
    ("BMP", ".bmp"),
    ("GIF", ".gif"),
])
def test_unsupported_raster_is_converted_to_jpeg(tmp_path, fmt, suffix):
    source = tmp_path / f"image{suffix}"
    Image.new("RGB", (120, 80), "green").save(source, fmt)
    prepared = prepare_platform_image(source, platform="wechat")
    try:
        assert prepared.is_temp is True
        assert prepared.path.suffix == ".jpg"
        with Image.open(prepared.path) as result:
            assert result.format == "JPEG"
    finally:
        _cleanup(prepared)


def test_alpha_image_is_converted_to_png(tmp_path):
    source = tmp_path / "alpha.webp"
    Image.new("RGBA", (80, 80), (255, 0, 0, 100)).save(source, "WEBP")
    prepared = prepare_platform_image(source, platform="wechat")
    try:
        assert prepared.path.suffix == ".png"
        with Image.open(prepared.path) as result:
            assert result.format == "PNG"
            assert result.mode == "RGBA"
    finally:
        _cleanup(prepared)


def test_svg_is_rasterized_to_png(tmp_path):
    source = tmp_path / "vector.svg"
    source.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80">'
        '<rect width="120" height="80" fill="#1677ff"/></svg>',
        encoding="utf-8",
    )
    prepared = prepare_platform_image(source, platform="wechat")
    try:
        assert prepared.path.suffix == ".png"
        with Image.open(prepared.path) as result:
            assert result.format == "PNG"
            assert result.size == (120, 80)
    finally:
        _cleanup(prepared)


def test_svg_with_wrong_remote_suffix_is_detected(tmp_path):
    source = tmp_path / "download.jpg"
    source.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">'
        '<circle cx="5" cy="5" r="5"/></svg>',
        encoding="utf-8",
    )
    prepared = prepare_platform_image(source, platform="wechat")
    try:
        assert prepared.path.suffix == ".png"
        with Image.open(prepared.path) as result:
            assert result.format == "PNG"
    finally:
        _cleanup(prepared)


def test_large_unsupported_image_is_compressed_below_limit(tmp_path):
    source = tmp_path / "large.bmp"
    # Noise prevents an unrealistically tiny solid-colour JPEG.
    image = Image.effect_noise((1200, 900), 100).convert("RGB")
    image.save(source, "BMP")
    prepared = prepare_platform_image(
        source, platform="wechat", max_bytes=120_000)
    try:
        assert prepared.path.stat().st_size <= 120_000
        with Image.open(prepared.path) as result:
            assert result.format == "JPEG"
    finally:
        _cleanup(prepared)
