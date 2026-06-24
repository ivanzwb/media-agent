from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config import Config
from app.pipeline.localize import local_media_file, normalize_url
from app.pipeline.narration import load_narration
from app.video.builder import build_video

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _noop(*_a, **_k) -> None:
    pass


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/" if p.scheme and p.netloc else url


def _download_videos(urls: list[str], dest: Path, config: Config,
                     progress=None, max_seconds: int = 40) -> dict[int, Path]:
    """Download original videos via yt-dlp, keeping their 1-based index (for
    media 'video:N'). Only the first `max_seconds` are fetched to save time.

    Note: embedding third-party video into your own output may carry copyright
    obligations — cite sources / obtain rights as appropriate.
    """
    emit = progress or _noop
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    out: dict[int, Path] = {}
    # Resolve already-localized videos directly; only remote ones need yt-dlp.
    remote: list[tuple[int, str]] = []
    for i, url in enumerate(urls, 1):
        local = local_media_file(url, config)
        if local:
            out[i] = local
        else:
            remote.append((i, url))
    if remote and not shutil.which("yt-dlp"):
        emit("  未安装 yt-dlp，跳过远程原视频片段（pip install yt-dlp）")
        return out
    for i, url in remote:
        emit(f"  下载原视频 #{i}…")
        template = str(dest / f"vid-{i}.%(ext)s")
        cmd = ["yt-dlp", "--no-playlist", "--quiet", "--no-warnings",
               "--download-sections", f"*0-{max_seconds}",
               "-f", "best[ext=mp4]/best", "--merge-output-format", "mp4",
               "-o", template, url]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        except (subprocess.SubprocessError, OSError) as e:
            emit(f"    失败：{e}")
            continue
        if r.returncode != 0:
            emit(f"    下载失败（可能需登录/不支持）：{url[:60]}")
            continue
        matches = sorted(dest.glob(f"vid-{i}.*"))
        if matches:
            out[i] = matches[0]
    return out


def _download_images(urls: list[str], dest: Path, config: Config,
                     progress=None) -> dict[int, Path]:
    """Resolve images to local files (keyed by 1-based index for media
    'image:N'). Local /media/ paths are used directly; remote URLs are
    downloaded as a fallback for older data."""
    emit = progress or _noop
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    out: dict[int, Path] = {}
    headers = {"User-Agent": _UA}
    for i, url in enumerate(urls, 1):
        local = local_media_file(url, config)
        if local:
            out[i] = local
            continue
        safe = normalize_url(url)
        if not safe:
            emit(f"  跳过无效配图地址 #{i}：{(url or '')[:60]}")
            continue
        try:
            data = httpx.get(
                safe, timeout=20.0, follow_redirects=True,
                headers={**headers, "Referer": _origin(safe)}).content
        except Exception as e:  # noqa: BLE001
            emit(f"  配图下载失败 #{i}：{e}")
            continue
        if not data:
            emit(f"  配图为空 #{i}")
            continue
        ext = ".jpg" if any(s in url.lower() for s in (".jpg", ".jpeg")) \
            else ".png"
        p = dest / f"img-{i}{ext}"
        p.write_bytes(data)
        out[i] = p
    return out


def build_explainer_video(draft_id: int, config: Config, progress=None) -> Path:
    """Compose the explainer video for a draft from its narration script
    (must be generated first) + downloaded article images."""
    emit = progress or _noop
    script = load_narration(draft_id, config)
    if not script or not script.get("scenes"):
        raise ValueError("请先生成讲解脚本+配音，再合成视频")

    work = config.videos_dir / f"draft-{draft_id}"
    imgs = script.get("images", [])
    if not imgs:
        emit("提示：正文未发现图片，画面会是纯色；可在归档页对该文章「重写」"
             "以带回原文配图后，重新生成脚本+配音再合成")
    emit("准备配图素材…")
    image_map = _download_images(imgs, work / "assets", config, progress=emit)

    # Only use videos actually referenced by a scene (media "video:N").
    referenced = any(str(s.get("media", "")).startswith("video:")
                     for s in script.get("scenes", []))
    video_map: dict[int, Path] = {}
    if referenced and script.get("videos"):
        emit("准备原视频片段…")
        video_map = _download_videos(script["videos"], work / "assets", config,
                                     progress=emit)

    out = work / "video.mp4"
    build_video(script, work, image_map, out, progress=emit,
                video_map=video_map)
    return out


def video_path(draft_id: int, config: Config) -> Path | None:
    p = config.videos_dir / f"draft-{draft_id}" / "video.mp4"
    return p if p.exists() else None
