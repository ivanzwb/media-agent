from __future__ import annotations

import json
import re

from app.llm.base import LLMProvider, Message
from app.models import Article, Draft

REWRITE_SYSTEM = (
    "你是资深自媒体编辑。基于且仅基于提供的原文事实进行改写，"
    "目标是通俗易懂、吸引眼球、有爆款潜质，但绝不可编造原文没有的数据、"
    "引用、结论或时间。不确定的内容不要写。"
)

REWRITE_INSTRUCTION = (
    "请把下面的原文改写成中文自媒体文章。只能依据原文事实。\n"
    "输出严格的 JSON，字段：\n"
    '  "title_candidates": [3 个吸睛但不虚假的标题],\n'
    '  "body_md": "Markdown 正文，含开头钩子、小标题、分点，适合手机阅读"\n'
    "不要输出 JSON 以外的任何内容。\n\n"
    "原文标题：{title}\n来源：{source}\n\n原文正文：\n{content}"
)

CHECK_INSTRUCTION = (
    "你是事实校验员。对照原文，找出改写稿中原文未提及或可能失真的具体说法。\n"
    "输出严格 JSON：{{\"flagged_claims\": [可疑说法的简短描述]}}。"
    "若全部忠实于原文，返回空数组。不要输出 JSON 以外内容。\n\n"
    "原文：\n{source_content}\n\n改写稿：\n{draft}"
)


_MAX_IMG = 12
_MAX_VID = 8
_PLACEHOLDER_RE = re.compile(r"\[{1,2}\s*(IMG|VID)\s*(\d+)\s*\]{1,2}", re.I)


def _unique(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _hint(url: str) -> str:
    """A short, human-readable hint for an asset (its file name)."""
    tail = url.rstrip("/").split("/")[-1].split("?")[0]
    return (tail or url)[:60]


def _video_embed(url: str) -> str:
    return (f'<iframe src="{url}" width="100%" height="420" '
            'frameborder="0" allowfullscreen></iframe>\n'
            f'[▶ 视频链接]({url})')


def _build_manifest(images: list[str], videos: list[str]):
    """Return (manifest_text, media_map) for placeholder-based insertion.

    media_map maps a token like "IMG1" -> ("img"|"vid", url).
    """
    imgs = _unique(images or [])[:_MAX_IMG]
    vids = _unique(videos or [])[:_MAX_VID]
    media_map: dict[str, tuple[str, str]] = {}
    lines: list[str] = []
    if imgs:
        lines.append(
            "【可用配图】请在 body_md 中与内容相关的段落之间插入下列占位符"
            "（每个独占一行，最多用一次，内容不相关就不要插入）：")
        for i, u in enumerate(imgs, 1):
            tok = f"IMG{i}"
            media_map[tok] = ("img", u)
            lines.append(f"[[{tok}]] - {_hint(u)}")
    if vids:
        lines.append("【可用视频】同理用下列占位符：")
        for i, u in enumerate(vids, 1):
            tok = f"VID{i}"
            media_map[tok] = ("vid", u)
            lines.append(f"[[{tok}]] - {_hint(u)}")
    if media_map:
        lines.append(
            "规则：原样保留占位符文字（如 [[IMG1]]），不要编造不存在的图片/"
            "视频；未使用的会自动附在文末。")
    return "\n".join(lines), media_map


def _apply_placeholders(body: str, media_map: dict[str, tuple[str, str]]) -> str:
    """Replace [[IMG1]]/[[VID1]] placeholders with real markdown/embeds.

    Unknown or leftover placeholders are removed.
    """
    def repl(m: re.Match) -> str:
        tok = f"{m.group(1).upper()}{m.group(2)}"
        item = media_map.get(tok)
        if not item:
            return ""
        kind, url = item
        if kind == "img":
            return f"\n\n![]({url})\n\n"
        return f"\n\n{_video_embed(url)}\n\n"

    return _PLACEHOLDER_RE.sub(repl, body)


def _media_block(images: list[str], videos: list[str], existing: str) -> str:
    """Append original images/videos so they survive the rewrite.

    Skips media whose URL already appears in the rewritten body to avoid
    duplicates.
    """
    imgs = [u for u in _unique(images or []) if u not in existing][:_MAX_IMG]
    vids = [u for u in _unique(videos or []) if u not in existing][:_MAX_VID]
    parts: list[str] = []
    if imgs:
        parts.append("\n\n## 配图（来自原文）\n")
        parts.extend(f"![]({u})" for u in imgs)
    if vids:
        parts.append("\n\n## 视频（来自原文）\n")
        parts.extend(_video_embed(u) for u in vids)
    return "\n".join(parts)


def _extract_json(text: str) -> dict | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


def rewrite(article: Article, provider: LLMProvider,
            platform: str = "master") -> Draft:
    videos = getattr(article, "videos", []) or []
    manifest_text, media_map = _build_manifest(article.images, videos)

    rewrite_prompt = REWRITE_INSTRUCTION.format(
        title=article.title, source=article.source_name,
        content=article.content_md[:6000])
    if manifest_text:
        rewrite_prompt += "\n\n" + manifest_text
    raw = provider.chat([
        Message(role="system", content=REWRITE_SYSTEM),
        Message(role="user", content=rewrite_prompt),
    ])
    parsed = _extract_json(raw)
    if parsed and parsed.get("title_candidates") and parsed.get("body_md"):
        title_candidates = parsed["title_candidates"]
        body_md = parsed["body_md"]
    else:
        title_candidates = [article.title]
        body_md = raw

    # Fact-check on the plain rewritten text (strip placeholders so injected
    # media doesn't confuse the checker).
    check_text = _PLACEHOLDER_RE.sub("", body_md)

    # Backfill placeholders the LLM inserted with real images/videos, then
    # append any remaining (unused) media at the end so nothing is lost.
    body_md = _apply_placeholders(body_md, media_map)
    media = _media_block(article.images, videos, body_md)
    body_with_source = (
        f"{body_md}{media}\n\n---\n**信息来源**："
        f"[{article.source_name}]({article.url})\n"
    )

    check_prompt = CHECK_INSTRUCTION.format(
        source_content=article.content_md[:6000], draft=check_text)
    check_raw = provider.chat([Message(role="user", content=check_prompt)])
    check_parsed = _extract_json(check_raw) or {}
    flagged = check_parsed.get("flagged_claims", []) or []

    return Draft(
        article_id=article.id or 0,
        platform=platform,
        title_candidates=title_candidates,
        body_md=body_with_source,
        topic=article.topic or "uncategorized",
        source_url=article.url,
        source_name=article.source_name,
        cover_image=None,
        flagged_claims=flagged,
    )
