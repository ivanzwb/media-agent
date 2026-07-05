"""Orchestrate publishing a Draft to a WeChat Official Account.

图文: markdown -> HTML, upload every image to WeChat, upload a cover as
permanent material (thumb), create a draft, optionally freepublish.
视频: upload the generated explainer mp4 as permanent material (10MB cap).
"""
from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.wechat.client import WeChatClient, WeChatError
from app.wechat.html import markdown_to_html, image_srcs, replace_image_srcs

logger = logging.getLogger(__name__)

_UPLOADIMG_MAX = 1_000_000      # media/uploadimg hard limit (<1MB)
_MATERIAL_IMG_MAX = 10_000_000  # add_material image limit (10MB)
_MATERIAL_VIDEO_MAX = 10_000_000  # add_material video limit (10MB, MP4)


def _plain_text(md: str) -> str:
    txt = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', md or '')       # images
    txt = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', txt)         # links
    txt = re.sub(r'[#*`>_\-]', '', txt)
    txt = re.sub(r'\s+', ' ', txt).strip()
    return txt


def _resolve_to_file(url: str, config) -> tuple[Path | None, bool]:
    """Resolve an image/media URL to a local file.

    Returns (path, is_temp). Handles local web paths (/media, /images,
    /videos) and best-effort download of remote http(s) images to a temp file.
    """
    if not url:
        return None, False
    if url.startswith("/media/"):
        from app.pipeline.localize import local_media_file
        return local_media_file(url, config), False
    if url.startswith("/images/"):
        p = (config.images_dir / url[len("/images/"):]).resolve()
        return (p if p.exists() else None), False
    if url.startswith("/videos/"):
        p = (config.videos_dir / url[len("/videos/"):]).resolve()
        return (p if p.exists() else None), False
    if url.startswith(("http://", "https://")):
        try:
            resp = httpx.get(url, timeout=30.0, follow_redirects=True)
            resp.raise_for_status()
            ext = Path(urlparse(url).path).suffix or ".jpg"
            fd, tmp = tempfile.mkstemp(suffix=ext)
            Path(tmp).write_bytes(resp.content)
            import os
            os.close(fd)
            return Path(tmp), True
        except Exception as exc:  # noqa: BLE001
            logger.warning("wechat: failed to download image %s: %s", url, exc)
            return None, False
    return None, False


def _upload_body_image(client: WeChatClient, path: Path) -> str | None:
    """Upload one body image; returns a WeChat-hosted URL or None."""
    size = path.stat().st_size
    if size <= _UPLOADIMG_MAX:
        try:
            return client.upload_article_image(path)
        except WeChatError as exc:
            logger.warning("uploadimg failed (%s), trying add_material", exc)
    if size <= _MATERIAL_IMG_MAX:
        try:
            return client.add_material("image", path).get("url")
        except WeChatError as exc:
            logger.warning("add_material image failed for %s: %s", path, exc)
    else:
        logger.warning("image too large for WeChat (%d bytes): %s", size, path)
    return None


def publish_article(client: WeChatClient, config, meta: dict,
                    mode: str = "draft") -> dict:
    """Publish a draft's 图文 to WeChat.

    ``mode``: "draft" (push to 草稿箱, default) or "publish" (draft + freepublish).
    Returns a JSON-able result dict; raises WeChatError on API failure.
    """
    titles = meta.get("title_candidates") or []
    title = (meta.get("title_cn") or (titles[0] if titles else "")
             or "无标题").strip()[:64]
    body_md = meta.get("body_md", "")
    html = markdown_to_html(body_md)

    # 1. Upload every body image, remap srcs.
    temps: list[Path] = []
    mapping: dict[str, str] = {}
    for src in image_srcs(html):
        path, is_temp = _resolve_to_file(src, config)
        if is_temp and path:
            temps.append(path)
        mapping[src] = _upload_body_image(client, path) or "" if path else ""
    html = replace_image_srcs(html, mapping)

    # 2. Cover (thumb) — required by draft/add. Prefer draft.cover_image,
    #    else fall back to the first resolvable body image.
    thumb_media_id = None
    cover_error = ""
    cover_name = meta.get("cover_image")
    cover_path = None
    if cover_name:
        cand = (config.images_dir / cover_name).resolve()
        # Tiny images (< 10 KB) are likely mock-generated placeholders
        # that WeChat's content filter rejects; treat as missing.
        if cand.exists() and cand.stat().st_size >= 10_000:
            cover_path = cand
    if cover_path is None:
        for src in image_srcs(markdown_to_html(body_md)):
            p, is_temp = _resolve_to_file(src, config)
            if p and p.exists():
                cover_path = p
                if is_temp:
                    temps.append(p)
                break

    # 3. Last resort: generate a visual-rich placeholder cover (900x500,
    #    gradient background + title + decorative shapes) so the WeChat
    #    push doesn't fail when no real cover / body image is available.
    #    A plain solid-color image gets rejected by WeChat's content filter.
    if cover_path is None:
        try:
            from PIL import Image, ImageDraw, ImageFont
            w, h = 900, 500
            # Gradient background (deep blue → darker teal)
            img = Image.new("RGB", (w, h))
            for y in range(h):
                r = int(10 + (y / h) * 20)
                g = int(20 + (y / h) * 60)
                b = int(50 + (y / h) * 40)
                for x in range(w):
                    img.putpixel((x, y), (r, g, b))
            draw = ImageDraw.Draw(img)
            # Decorative accent bar at top
            for x in range(w):
                for dy in range(4):
                    img.putpixel((x, dy), (0, 180, 160))
            # Subtle glow circles
            for cx, cy, rad, alpha in [(700, 120, 80, 30), (150, 380, 60, 20)]:
                for dx in range(-rad, rad + 1):
                    for dy in range(-rad, rad + 1):
                        if dx * dx + dy * dy <= rad * rad:
                            px, py = cx + dx, cy + dy
                            if 0 <= px < w and 0 <= py < h:
                                orig = img.getpixel((px, py))
                                blend = tuple(
                                    min(255, c + alpha) for c in orig)
                                img.putpixel((px, py), blend)
            # Title text
            txt = title or "Media Agent"
            font = None
            for fname in ("C:/Windows/Fonts/msyh.ttc",
                          "C:/Windows/Fonts/simhei.ttf",
                          "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                          "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"):
                try:
                    font = ImageFont.truetype(fname, 38)
                    break
                except (OSError, IOError):
                    continue
            if font:
                lines: list[str] = []
                cur = ""
                for ch in txt:
                    test = cur + ch
                    bbox = draw.textbbox((0, 0), test, font=font)
                    if bbox[2] > w - 60:
                        lines.append(cur)
                        cur = ch
                    else:
                        cur = test
                if cur:
                    lines.append(cur)
                y_pos = 180
                for line in lines[:3]:
                    bbox = draw.textbbox((0, 0), line, font=font)
                    tw = bbox[2] - bbox[0]
                    # Shadow
                    draw.text(((w - tw) / 2 + 2, y_pos + 2), line,
                              fill=(0, 0, 0, 120), font=font)
                    draw.text(((w - tw) / 2, y_pos), line,
                              fill=(240, 240, 240), font=font)
                    y_pos += 52
            else:
                draw.text((w / 2, 230), txt[:40], fill=(240, 240, 240),
                          anchor="mm")
            import tempfile as tmp_module
            fd, tmp = tmp_module.mkstemp(suffix=".png")
            img.save(tmp, "PNG")
            import os
            os.close(fd)
            cover_path = Path(tmp)
            temps.append(cover_path)
        except Exception:
            pass  # if PIL isn't available, give up

    if cover_path is not None and cover_path.stat().st_size <= _MATERIAL_IMG_MAX:
        try:
            result = client.add_material("image", cover_path)
            thumb_media_id = result.get("media_id")
            if not thumb_media_id:
                logger.warning("add_material returned no media_id: %s", result)
                cover_error = "上传成功但未返回 media_id"
        except WeChatError as exc:
            logger.warning("cover upload failed: %s", exc)
            # Surface the WeChat error to the user
            cover_error = f"微信接口错误 {exc.errcode}: {exc.errmsg}"
    else:
        if cover_path is not None and cover_path.stat().st_size > _MATERIAL_IMG_MAX:
            cover_error = f"封面图片过大 ({cover_path.stat().st_size} bytes > 10MB 限制)"

    # cleanup temp downloads
    for t in temps:
        try:
            t.unlink()
        except OSError:
            pass

    if not thumb_media_id:
        msg = "缺少封面图：请先为草稿设置封面，或确保正文包含可用图片"
        if cover_error:
            msg += f"（{cover_error}）"
        return {"ok": False, "error": msg}

    article = {
        "title": title,
        "author": (config.wechat_author or "").strip(),
        "digest": _plain_text(body_md)[:120],
        "content": html,
        "thumb_media_id": thumb_media_id,
        "content_source_url": meta.get("source_url") or "",
        "need_open_comment": 0,
        "only_fans_can_comment": 0,
    }
    draft_media_id = client.add_draft([article])
    result = {"ok": True, "draft_media_id": draft_media_id, "title": title,
              "mode": mode}
    if mode == "publish":
        result["publish_id"] = client.freepublish_submit(draft_media_id)
    return result


def upload_video(client: WeChatClient, config, draft_id: int,
                 meta: dict) -> dict:
    """Upload a draft's generated explainer video as permanent material.

    Note: the OA API cannot push a standalone video into the feed; this makes
    the video available in 素材库 for manual insertion / mass-send. For true
    video publishing use the 视频号 (Channels) API instead.
    """
    from app.pipeline.video import video_path
    mp4 = video_path(draft_id, config)
    if mp4 is None:
        return {"ok": False, "error": "尚未生成讲解视频，请先在视频页生成。"}
    size = mp4.stat().st_size
    if size > _MATERIAL_VIDEO_MAX:
        return {"ok": False,
                "error": f"视频 {size // 1_000_000}MB 超过公众号素材上限 10MB。"
                         "请缩短视频/降码率,或改用视频号(Channels)API。"}
    titles = meta.get("title_candidates") or []
    title = (meta.get("title_cn") or (titles[0] if titles else "")
             or "视频").strip()[:64]
    data = client.add_material("video", mp4, title=title,
                               introduction=_plain_text(meta.get("body_md",
                                                                 ""))[:120])
    return {"ok": True, "media_id": data.get("media_id"), "title": title,
            "note": "视频已上传到公众号素材库。OA 接口不支持将独立视频直接发到信息流，"
                    "请在公众号后台从素材库插入图文，或使用视频号。"}
