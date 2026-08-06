from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from urllib.parse import urlparse

import json as _json

from app.config import Config
from app.pipeline.localize import local_media_file
from app.pipeline.narration import load_narration
from app.video.builder import build_video

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _noop(*_a, **_k) -> None:
    pass


def _download_remote_image(url: str, work: Path, emit) -> Path | None:
    """Best-effort download of a remote article image into the draft's video
    work dir so it can be used as a scene background."""
    try:
        import httpx
        work.mkdir(parents=True, exist_ok=True)
        ext = Path(urlparse(url).path).suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
            ext = ".jpg"
        name = "dl-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:12] + ext
        dest = work / name
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        r = httpx.get(url, timeout=30.0, follow_redirects=True,
                      headers={"User-Agent": _UA})
        r.raise_for_status()
        dest.write_bytes(r.content)
        return dest
    except Exception as exc:  # noqa: BLE001 - never block the build
        emit(f"    远程配图下载失败：{url[:60]}… ({exc})")
        return None


def _download_remote_video(url: str, work: Path, emit) -> Path | None:
    """Best-effort download of a remote video into the draft's video work dir
    so it can be used as a scene background. Direct .mp4/.webm/etc. go through
    the httpx strategy; platform embeds (YouTube/Bilibili/…) fall back to
    yt-dlp."""
    try:
        from app.pipeline.localize import _download_video
        work.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        dest = work / f"dl-{key}"
        dest.mkdir(parents=True, exist_ok=True)
        p = _download_video(url, dest, 1, emit)
        if p and p.exists():
            return p
        emit(f"    远程视频下载失败：{url[:60]}…")
        return None
    except Exception as exc:  # noqa: BLE001 - never block the build
        emit(f"    远程视频下载失败：{url[:60]}… ({exc})")
        return None


def _resolve_one(url: str, config: Config, work: Path | None,
                 emit, download: bool, kind: str = "image") -> Path | None:
    if not url:
        return None
    # Drafts written by the rewriter/localizer store file-relative paths
    # (../../media/…, ../../images/…). Absolutize them the same way the WeChat
    # publisher does so they resolve to the served local files.
    if url.startswith("../../media/"):
        url = "/media/" + url[len("../../media/"):]
    elif url.startswith("../../images/"):
        url = "/images/" + url[len("../../images/"):]

    if url.startswith("/media/"):
        return local_media_file(url, config)
    if url.startswith("/images/"):
        p = (config.images_dir / url[len("/images/"):]).resolve()
        return p if p.exists() else None
    if url.startswith("/videos/"):
        p = (config.videos_dir / url[len("/videos/"):]).resolve()
        return p if p.exists() else None
    if download and work is not None and url.startswith(("http://", "https://")):
        if kind == "video":
            return _download_remote_video(url, work, emit)
        return _download_remote_image(url, work, emit)
    return None


def _resolve_media(urls: list[str], config: Config, work: Path | None = None,
                   emit=None, download: bool = False,
                   kind: str = "image") -> dict[int, Path]:
    """Resolve media URLs (index→Path). Handles absolute /media|/images|/videos
    paths AND file-relative ../../media|../../images (as drafts store them). When
    *download* is set, remote http(s) media are fetched into *work* so remote
    URLs still become scene backgrounds (kind="video" routes to the video
    downloader chain)."""
    emit = emit or _noop
    out: dict[int, Path] = {}
    for i, url in enumerate(urls, 1):
        p = _resolve_one(url, config, work, emit, download, kind)
        if p:
            out[i] = p
    return out


def _migrate_script(script: dict) -> bool:
    """Convert old script format (intro_audio/outro_audio at top level) to
    new format (intro/outro as scenes with type field). Returns True if
    migration happened (caller should persist)."""
    scenes = script.get("scenes", [])
    # Already migrated — first scene has a type marker
    if scenes and scenes[0].get("type") in ("intro", "outro"):
        return False
    has_intro = "intro_audio" in script
    has_outro = "outro_audio" in script
    if not has_intro and not has_outro:
        return False

    title = script.get("title") or script.get("article_title") or ""
    if has_intro:
        scenes.insert(0, {
            "type": "intro",
            "narration": title,
            "visual": "片头画面",
            "media": "none",
            "audio": script.pop("intro_audio", None),
        })
    if has_outro:
        scenes.append({
            "type": "outro",
            "narration": "感谢观看，我们下期再见！",
            "visual": "片尾画面",
            "media": "none",
            "audio": script.pop("outro_audio", None),
        })
    return True


def build_explainer_video(draft_id: int, config: Config, progress=None) -> Path:
    """Compose the explainer video for a draft from its narration script
    (must be generated first) + article images (localised at fetch time)."""
    emit = progress or _noop
    script = load_narration(draft_id, config)
    if not script or not script.get("scenes"):
        raise ValueError("请先生成讲解脚本+配音，再合成视频")

    work = config.videos_dir / f"draft-{draft_id}"
    imgs = script.get("images", [])
    if not imgs:
        emit("提示：正文未发现图片，画面会是纯色；可在归档页对该文章「重写」"
             "以带回原文配图后，重新生成脚本+配音再合成")
    emit("查找配图素材…")
    # Resolve article images: absolute /media, file-relative ../../media (as
    # drafts store them), or remote URLs (downloaded into the work dir).
    image_map = _resolve_media(imgs, config, work=work, emit=emit,
                               download=True)
    if imgs and not image_map:
        emit(f"提示：{len(imgs)} 张配图均无法解析为本地文件，画面将使用纯色背景")

    # Only use videos actually referenced by a scene (media "video:N").
    referenced = any(str(s.get("media", "")).startswith("video:")
                     for s in script.get("scenes", []))
    video_map: dict[int, Path] = {}
    if referenced and script.get("videos"):
        emit("查找本地原视频…")
        video_map = _resolve_media(script["videos"], config, work=work,
                                   emit=emit, download=True, kind="video")
        if not video_map:
            emit("提示：视频素材均无法解析为本地文件，相关分镜将用图片背景")

    # Migrate old-format scripts (intro_audio/outro_audio at top level)
    if _migrate_script(script):
        (work / "script.json").write_text(
            _json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")

    out = work / "video.mp4"
    build_video(script, work, image_map, out, progress=emit,
                video_map=video_map, fit=(config.video_fit or "fit"),
                brand_name=(config.video_brand_name or "Media Agent"),
                avatar=_avatar_spec(config, emit))
    return out


def _avatar_spec(config: Config, emit):
    """Build the digital-human presenter spec from config (or None)."""
    if not config.avatar_enabled:
        return None
    from app.video.avatar import AvatarSpec, sadtalker_available
    from app.video.sadtalker_setup import (
        checkpoint_dir, resolve_sadtalker_dir, resolve_sadtalker_python)
    img = (config.avatar_image or "").strip()
    if not img:
        emit("数字人已启用，但未设置主播头像（设置→视频），本次跳过")
        return None
    img_path = Path(img)
    if not img_path.is_absolute():
        img_path = config.avatar_dir / img
    if not img_path.exists():
        emit(f"数字人主播头像不存在：{img_path.name}，本次跳过")
        return None
    spec = AvatarSpec(
        enabled=True, image=img_path,
        provider=(config.avatar_provider or "sadtalker"),
        position=(config.avatar_position or "pip"),
        sadtalker_dir=resolve_sadtalker_dir(config.data_dir),
        sadtalker_python=resolve_sadtalker_python(config.data_dir),
        checkpoint_dir=str(checkpoint_dir(config.data_dir)),
    )
    if spec.provider == "sadtalker" and not sadtalker_available(spec):
        emit("提示：SadTalker runtime 或模型未就绪（将用静态头像）。"
             "请在设置中完成数字人安装。")
    return spec


def video_path(draft_id: int, config: Config) -> Path | None:
    p = config.videos_dir / f"draft-{draft_id}" / "video.mp4"
    return p if p.exists() else None
