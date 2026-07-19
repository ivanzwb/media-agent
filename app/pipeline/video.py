from __future__ import annotations

from pathlib import Path

import json as _json

from app.config import Config
from app.pipeline.localize import local_media_file
from app.pipeline.narration import load_narration
from app.video.builder import build_video


def _noop(*_a, **_k) -> None:
    pass


def _resolve_media(urls: list[str], config: Config) -> dict[int, Path]:
    """Resolve already-localised media URLs to local Paths.

    All assets are expected to be already localised when the article was
    fetched — the video stage never downloads from the network.  Any URL
    that does not resolve to a local file is silently skipped.
    """
    out: dict[int, Path] = {}
    for i, url in enumerate(urls, 1):
        local = local_media_file(url, config)
        if local:
            out[i] = local
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
    emit("查找本地配图素材…")
    image_map = _resolve_media(imgs, config)

    # Only use videos actually referenced by a scene (media "video:N").
    referenced = any(str(s.get("media", "")).startswith("video:")
                     for s in script.get("scenes", []))
    video_map: dict[int, Path] = {}
    if referenced and script.get("videos"):
        emit("查找本地原视频…")
        video_map = _resolve_media(script["videos"], config)

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
        sadtalker_dir=config.sadtalker_dir,
        sadtalker_python=config.sadtalker_python,
    )
    if spec.provider == "sadtalker" and not sadtalker_available(spec):
        emit("提示：未检测到 SadTalker（将用静态头像）。"
             "配置 SadTalker 目录后可启用口型同步。")
    return spec


def video_path(draft_id: int, config: Config) -> Path | None:
    p = config.videos_dir / f"draft-{draft_id}" / "video.mp4"
    return p if p.exists() else None
