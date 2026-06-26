from __future__ import annotations

import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config import Config
from app.pipeline.localize import local_media_file, normalize_url
from app.pipeline.narration import load_narration
from app.video.builder import build_video
from app.media.downloader import MediaDownloader
from app.media.strategies import (
    HttpxDirectStrategy,
    UrlTransformStrategy,
    HttpxDirectVideoStrategy,
    YtDlpStrategy,
)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _noop(*_a, **_k) -> None:
    pass


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/" if p.scheme and p.netloc else url


def _download_videos(urls: list[str], dest: Path, config: Config,
                     progress=None, max_seconds: int = 40) -> dict[int, Path]:
    """Download original videos via the strategy-based fallback chain,
    keeping their 1-based index (for media 'video:N').

    Note: embedding third-party video into your own output may carry copyright
    obligations — cite sources / obtain rights as appropriate.
    """
    emit = progress or _noop
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    downloader = MediaDownloader("video")
    downloader.add_strategy(HttpxDirectVideoStrategy())
    downloader.add_strategy(YtDlpStrategy())

    out: dict[int, Path] = {}
    for i, url in enumerate(urls, 1):
        local = local_media_file(url, config)
        if local:
            out[i] = local
            continue
        emit(f"  下载原视频 #{i}…")
        p = downloader.download(url, dest, i, emit, max_seconds=max_seconds)
        if p:
            out[i] = p
    return out


def _download_images(urls: list[str], dest: Path, config: Config,
                     progress=None) -> dict[int, Path]:
    """Resolve images to local files (keyed by 1-based index for media
    'image:N'). Local /media/ paths are used directly; remote URLs are
    downloaded via the strategy-based fallback chain."""
    emit = progress or _noop
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    downloader = MediaDownloader("image")
    downloader.add_strategy(HttpxDirectStrategy())
    downloader.add_strategy(UrlTransformStrategy())

    def fetch(i: int, url: str) -> Path | None:
        local = local_media_file(url, config)
        if local:
            return local
        return downloader.download(url, dest, i, emit)

    out: dict[int, Path] = {}
    workers = max(1, config.workers)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch, i, u): i for i, u in enumerate(urls, 1)}
        for f in as_completed(futs):
            i = futs[f]
            try:
                p = f.result()
            except Exception as e:  # noqa: BLE001
                emit(f"  配图下载失败 #{i}：{e}")
                p = None
            if p:
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
                video_map=video_map, fit=(config.video_fit or "fit"))
    return out


def video_path(draft_id: int, config: Config) -> Path | None:
    p = config.videos_dir / f"draft-{draft_id}" / "video.mp4"
    return p if p.exists() else None
