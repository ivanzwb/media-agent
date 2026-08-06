"""Generic half-automatic video caption builder (platform-agnostic).

Given a draft and a platform's caption *style* prompt, produce a short
vertical-video caption (title + spoken-style description + hashtags) via the
LLM, with a deterministic fallback so it never blocks on the LLM. Used by the
half-auto video flows (视频号, 头条, …) which prepare copy + open the creator
page for the user to finish manually (no account automation).
"""
from __future__ import annotations

import re

from app.llm.base import LLMProvider, Message


def _plain_text(md: str) -> str:
    t = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', md or '')       # images
    t = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', t)          # links -> text
    t = re.sub(r'\[\[VIDEO:\d+\]\]', '', t)
    t = re.sub(r'[#*`>_]', '', t)
    return re.sub(r'\s+', ' ', t).strip()


def _normalize_tags(tags) -> list[str]:
    out: list[str] = []
    for t in tags or []:
        t = str(t).strip()
        if not t:
            continue
        if not t.startswith("#"):
            t = "#" + t
        if t not in out:
            out.append(t)
    return out[:6]


def build_video_caption(meta: dict, provider: LLMProvider | None,
                        style_prompt: str) -> dict:
    """Return {title, description, hashtags, caption} for a short video."""
    titles = meta.get("title_candidates") or []
    base_title = (meta.get("title_cn") or (titles[0] if titles else "")
                  or "").strip()
    body = meta.get("body_md", "")
    topic = (meta.get("topic") or "").strip()

    parsed = None
    if provider is not None:
        try:
            from app.pipeline.rewriter import _extract_json
            instruction = (
                "基于且仅基于下面的主稿，不要编造事实。输出严格 JSON：\n"
                '{"title": "吸睛短标题(<=22字)", '
                '"description": "2-4 句口语化简介", '
                '"hashtags": ["#标签1", "#标签2"]}\n'
                "不要输出 JSON 以外的任何内容。\n\n"
                f"原标题：{base_title}\n\n主稿：\n{body[:3000]}"
            )
            # 简介是发出去的文案，而且要求「口语化」——正是「说实话」「不得
            # 不说」这类假口语最容易冒出来的地方。只注规则，不做正则清洗：
            # 两三句话里删掉一个短语，留下的多半是个病句。
            from app.pipeline.anti_slop import ANTI_SLOP_SYSTEM_INSTRUCTION
            raw = provider.chat([
                Message(role="system",
                        content=f"{style_prompt}\n\n"
                                f"{ANTI_SLOP_SYSTEM_INSTRUCTION}"),
                Message(role="user", content=instruction),
            ])
            parsed = _extract_json(raw)
        except Exception:                          # noqa: BLE001
            parsed = None

    if parsed and parsed.get("title") and parsed.get("description"):
        title = str(parsed["title"]).strip()
        description = str(parsed["description"]).strip()
        hashtags = _normalize_tags(parsed.get("hashtags"))
    else:
        title = base_title or "新视频"
        description = _plain_text(body)[:140] or base_title
        hashtags = _normalize_tags([topic] if topic else [])

    caption = description
    if hashtags:
        caption = f"{description}\n{' '.join(hashtags)}"
    return {"title": title[:64], "description": description,
            "hashtags": hashtags, "caption": caption}
