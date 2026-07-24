"""Normalize images to formats accepted by publishing platforms."""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps


class ImageConversionError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedImage:
    path: Path
    is_temp: bool = False


_SUPPORTED_FORMATS = {
    "wechat": {"JPEG", "PNG"},
    "toutiao": {"JPEG", "PNG"},
}
_MAX_DIMENSION = 4096


def _temporary_path(suffix: str) -> Path:
    fd, name = tempfile.mkstemp(prefix="media-agent-publish-", suffix=suffix)
    os.close(fd)
    return Path(name)


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info)


def _flatten(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    background = Image.new("RGB", rgba.size, "white")
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


def _is_svg(path: Path) -> bool:
    if path.suffix.lower() in (".svg", ".svgz"):
        return True
    try:
        with path.open("rb") as handle:
            prefix = handle.read(2048).decode("utf-8", errors="ignore")
    except OSError:
        return False
    normalized = prefix.lstrip("\ufeff \t\r\n").lower()
    return normalized.startswith("<svg") or (
        normalized.startswith("<?xml") and "<svg" in normalized)


def _open_image(path: Path) -> Image.Image:
    if _is_svg(path):
        try:
            from resvg_py import svg_to_bytes
            from io import BytesIO

            png = svg_to_bytes(
                svg_path=str(path),
                background="transparent",
                resources_dir=str(path.parent),
            )
            image = Image.open(BytesIO(png))
            image.load()
            return image
        except Exception as exc:  # noqa: BLE001
            raise ImageConversionError(f"SVG 转换失败：{exc}") from exc
    try:
        image = Image.open(path)
        image.seek(0)  # animated images publish as their first frame
        image.load()
        return image
    except Exception as exc:  # noqa: BLE001
        raise ImageConversionError(f"无法读取图片 {path.name}：{exc}") from exc


def _save_png(image: Image.Image, target: Path) -> None:
    mode = "RGBA" if _has_alpha(image) else "RGB"
    image.convert(mode).save(target, "PNG", optimize=True)


def _save_jpeg_under_limit(image: Image.Image, target: Path,
                           max_bytes: int | None) -> None:
    rgb = _flatten(image) if _has_alpha(image) else image.convert("RGB")
    working = rgb
    for scale_round in range(6):
        for quality in (92, 85, 76, 66, 56, 46, 36):
            working.save(
                target, "JPEG", quality=quality, optimize=True,
                progressive=True)
            if max_bytes is None or target.stat().st_size <= max_bytes:
                return
        if working.width <= 320 or working.height <= 320:
            break
        ratio = 0.82
        working = working.resize(
            (max(1, int(working.width * ratio)),
             max(1, int(working.height * ratio))),
            Image.Resampling.LANCZOS,
        )
    raise ImageConversionError(
        f"图片转换后仍超过平台限制 {max_bytes} bytes")


def prepare_platform_image(path: Path, *, platform: str,
                           max_bytes: int | None = None) -> PreparedImage:
    """Return a JPEG/PNG image compatible with *platform*.

    Existing supported files are reused when they fit the size limit. WebP,
    GIF, BMP, TIFF, AVIF and SVG are converted to a temporary JPEG/PNG.
    Animated images use their first frame. Callers must delete the returned
    path when ``is_temp`` is true.
    """
    source = Path(path)
    supported = _SUPPORTED_FORMATS.get(platform, {"JPEG", "PNG"})
    force_convert = _is_svg(source)
    image = _open_image(source)
    source_format = (getattr(image, "format", None) or "").upper()
    needs_resize = (
        image.width > _MAX_DIMENSION or image.height > _MAX_DIMENSION)
    image = ImageOps.exif_transpose(image)
    if needs_resize:
        image.thumbnail(
            (_MAX_DIMENSION, _MAX_DIMENSION), Image.Resampling.LANCZOS)

    if (source_format in supported
            and (max_bytes is None or source.stat().st_size <= max_bytes)
            and not needs_resize
            and not force_convert):
        return PreparedImage(source, False)

    preserve_alpha = _has_alpha(image) and "PNG" in supported
    if preserve_alpha:
        target = _temporary_path(".png")
        try:
            _save_png(image, target)
            if max_bytes is None or target.stat().st_size <= max_bytes:
                return PreparedImage(target, True)
            target.unlink(missing_ok=True)
        except Exception:
            target.unlink(missing_ok=True)
            raise

    if "JPEG" not in supported:
        raise ImageConversionError(
            f"{platform} 不支持图片格式 {source_format or source.suffix}")
    target = _temporary_path(".jpg")
    try:
        _save_jpeg_under_limit(image, target, max_bytes)
        return PreparedImage(target, True)
    except Exception:
        target.unlink(missing_ok=True)
        raise
