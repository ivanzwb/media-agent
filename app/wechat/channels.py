"""WeChat Channels (视频号) half-automatic publish helper.

WeChat provides NO server-side API to publish a video to 视频号 (verified: the
视频号助手 API only exposes live/lead/e-commerce data, not video posting). So
this module only *prepares* the publish: it generates a 视频号-style caption
(short title + spoken-style description + hashtags) from the draft. The web UI
then copies the caption to the clipboard and opens the 视频号助手 create page,
where the user drags in the video file and pastes — no account automation, no
ban risk.
"""
from __future__ import annotations

import re

from app.llm.base import LLMProvider, Message

# 视频号助手 "发表" page — opened in a new tab for the user to finish manually.
CHANNELS_CREATE_URL = "https://channels.weixin.qq.com/platform/post/create"

_SYSTEM = (
    "你是微信视频号文案编辑。视频号是竖屏短视频平台，简介要口语化、简短有信息量，"
    "并带 2-4 个 #话题标签。不要浮夸、不要绝对化用语。"
)


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


def build_channels_caption(meta: dict,
                           provider: LLMProvider | None = None) -> dict:
    """Build a 视频号 caption from a draft.

    Returns {title, description, hashtags, caption}. Uses the LLM when
    available; otherwise falls back to deterministic text from the draft so it
    always returns something usable (and never blocks on the LLM).
    """
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
            raw = provider.chat([
                Message(role="system", content=_SYSTEM),
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
        # deterministic fallback
        title = base_title or "新视频"
        description = _plain_text(body)[:140] or base_title
        hashtags = _normalize_tags([topic] if topic else [])

    caption = description
    if hashtags:
        caption = f"{description}\n{' '.join(hashtags)}"
    return {"title": title[:64], "description": description,
            "hashtags": hashtags, "caption": caption}
