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
    rewrite_prompt = REWRITE_INSTRUCTION.format(
        title=article.title, source=article.source_name,
        content=article.content_md[:6000])
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

    body_with_source = (
        f"{body_md}\n\n---\n**信息来源**："
        f"[{article.source_name}]({article.url})\n"
    )

    check_prompt = CHECK_INSTRUCTION.format(
        source_content=article.content_md[:6000], draft=body_md)
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
