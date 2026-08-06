from __future__ import annotations

import json
import re

from app.llm.base import LLMProvider, Message
from app.pipeline.anti_slop import ANTI_SLOP_SYSTEM_INSTRUCTION

_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
_IFRAME_RE = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I)
_LINK_VIDEO_RE = re.compile(r"\[\u25b6[^\]]*\]\(([^)\s]+)\)")  # [▶ ...](url)

# 旁白是要念出来给人听的，「口语化」的要求又恰好把模型往「说实话」「不得不
# 说」「敲黑板」上引——那不是口语，是模仿口语的腔调，念出来比书面语更假。
SCRIPT_SYSTEM = (
    "你是资深短视频编导。把图文稿改写成口播讲解视频的分镜脚本，"
    "语言口语化、有节奏、适合配音，只基于稿件事实，不要编造。\n\n"
    + ANTI_SLOP_SYSTEM_INSTRUCTION
)

SCRIPT_INSTRUCTION = (
    "请把下面的文章改写成讲解视频分镜脚本。\n"
    "输出严格 JSON 对象：顶层只有一个键 scenes，值为数组；数组每个元素包含"
    "三个字段——narration（该分镜的口播旁白，1-3句）、visual（画面要点或字幕"
    "短语）、media（引用下方素材：写 image:N 或 video:N，没有合适素材写 none）。\n"
    "分镜数量控制在 {max_scenes} 个以内。不要输出 JSON 以外的任何内容。\n\n"
    "标题：{title}\n\n正文：\n{body}\n\n{manifest}"
)


def _strip_media_markup(text: str) -> str:
    text = _IMG_RE.sub("", text)
    text = re.sub(r"<iframe[^>]*>.*?</iframe>", "", text, flags=re.I | re.S)
    return text


def extract_media(body_md: str) -> tuple[list[str], list[str]]:
    """Return (image_urls, video_urls) referenced in a draft body."""
    images: list[str] = []
    for u in _IMG_RE.findall(body_md):
        if u not in images:
            images.append(u)
    videos: list[str] = []
    for u in _IFRAME_RE.findall(body_md) + _LINK_VIDEO_RE.findall(body_md):
        if u not in videos:
            videos.append(u)
    return images, videos


def _manifest(images: list[str], videos: list[str]) -> str:
    lines: list[str] = []
    if images:
        lines.append("可用图片素材（用 image:N 引用）：")
        for i, u in enumerate(images, 1):
            lines.append(f"  image:{i} = {u}")
    if videos:
        lines.append("可用视频素材（用 video:N 引用）：")
        for i, u in enumerate(videos, 1):
            lines.append(f"  video:{i} = {u}")
    return "\n".join(lines)


def _parse_scenes(raw: str) -> list[dict]:
    def _coerce(obj) -> list[dict]:
        scenes = obj.get("scenes") if isinstance(obj, dict) else obj
        out: list[dict] = []
        if isinstance(scenes, list):
            for s in scenes:
                if isinstance(s, dict) and str(s.get("narration", "")).strip():
                    out.append({
                        "narration": str(s["narration"]).strip(),
                        "visual": str(s.get("visual", "")).strip(),
                        "media": str(s.get("media", "none")).strip() or "none",
                    })
        return out

    try:
        return _coerce(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return _coerce(json.loads(m.group(0)))
        except json.JSONDecodeError:
            pass
    return []


def _fallback_scenes(body_md: str, images: list[str],
                     max_scenes: int) -> list[dict]:
    """Split the body into paragraphs as narration; round-robin images."""
    text = _strip_media_markup(body_md)
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    paras = [re.sub(r"^#+\s*", "", p) for p in paras]
    paras = [p for p in paras if p][:max_scenes]
    scenes: list[dict] = []
    for i, p in enumerate(paras):
        media = f"image:{(i % len(images)) + 1}" if images else "none"
        scenes.append({"narration": p[:200], "visual": p[:30], "media": media})
    return scenes


def build_script(body_md: str, title: str, provider: LLMProvider,
                 max_scenes: int = 12) -> dict:
    images, videos = extract_media(body_md)
    clean_body = _strip_media_markup(body_md)[:6000]
    prompt = SCRIPT_INSTRUCTION.format(
        max_scenes=max_scenes, title=title or "",
        body=clean_body, manifest=_manifest(images, videos))
    raw = provider.chat([
        Message(role="system", content=SCRIPT_SYSTEM),
        Message(role="user", content=prompt),
    ])
    scenes = _parse_scenes(raw)
    if not scenes:
        scenes = _fallback_scenes(body_md, images, max_scenes)
    if not scenes:
        # Last-resort fallback: one generic scene
        scenes = [{"narration": body_md[:200] if body_md else title,
                   "visual": body_md[:30] if body_md else title,
                   "media": "none"}]
    return {"title": title, "scenes": scenes[:max_scenes],
            "images": images, "videos": videos}
