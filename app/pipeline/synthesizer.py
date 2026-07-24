from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.llm.base import LLMProvider, Message
from app.models import Article
from app.pipeline.anti_slop import ANTI_SLOP_SYSTEM_INSTRUCTION, post_process
from app.pipeline.rewriter import _extract_json, _extract_json_fallback

_SOURCE_CHARS = 2500
_TOTAL_SOURCE_CHARS = 18000


@dataclass
class SynthesisResult:
    title_candidates: list[str]
    body_md: str
    citations: list[dict] = field(default_factory=list)
    flagged_claims: list[str] = field(default_factory=list)


def _source_block(articles: list[Article]) -> str:
    blocks: list[str] = []
    used = 0
    for index, article in enumerate(articles, 1):
        remaining = _TOTAL_SOURCE_CHARS - used
        if remaining <= 0:
            break
        content = (article.content_md or article.raw_summary or "")[
            :min(_SOURCE_CHARS, remaining)]
        used += len(content)
        blocks.append(
            f"[{index}] 标题：{article.title}\n"
            f"来源：{article.source_name}\n"
            f"URL：{article.url}\n"
            f"正文：\n{content}"
        )
    return "\n\n---\n\n".join(blocks)


def _coerce_citations(value, source_count: int) -> list[dict]:
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        raw_indexes = item.get("source_indexes") or item.get("sources") or []
        indexes: list[int] = []
        for raw in raw_indexes if isinstance(raw_indexes, list) else [raw_indexes]:
            try:
                index = int(raw)
            except (TypeError, ValueError):
                continue
            if 1 <= index <= source_count and index not in indexes:
                indexes.append(index)
        claim = str(item.get("claim") or "").strip()
        if claim and indexes:
            out.append({
                "claim": claim,
                "source_indexes": indexes,
                "quote": str(item.get("quote") or "").strip(),
            })
    return out


def _link_citation_markers(body: str, articles: list[Article]) -> str:
    def replace(match: re.Match) -> str:
        index = int(match.group(1))
        if not 1 <= index <= len(articles):
            return match.group(0)
        return f"[[{index}]]({articles[index - 1].url})"

    return re.sub(r"(?<!\[)\[(\d+)\](?![\]\(])", replace, body)


def _references(articles: list[Article]) -> str:
    lines = ["## 参考文献"]
    for index, article in enumerate(articles, 1):
        lines.append(
            f"{index}. [{article.title}]({article.url}) — {article.source_name}")
    return "\n".join(lines)


def fact_check_multi(body_md: str, articles: list[Article],
                     provider: LLMProvider) -> list[str]:
    prompt = (
        "你是严格的事实核查员。请逐条核对综合稿中的数据、结论、引语和时间，"
        "判断它们能否被给出的参考资料支持。只输出 JSON："
        '{"flagged_claims":["无法支持或相互矛盾的表述"]}。'
        "完全有依据时输出空数组。不要输出解释。\n\n"
        f"参考资料：\n{_source_block(articles)}\n\n综合稿：\n{body_md[:12000]}"
    )
    raw = provider.chat([Message(role="user", content=prompt)])
    parsed = _extract_json(raw) or {}
    claims = parsed.get("flagged_claims") or []
    return [str(claim).strip() for claim in claims if str(claim).strip()]


def synthesize(topic: str, articles: list[Article], provider: LLMProvider,
               *, style=None, lang: str = "zh") -> SynthesisResult:
    if len(articles) < 2:
        raise ValueError("多源综合至少需要 2 篇有效参考资料")

    style_prompt = (
        style.prompt if style is not None
        else "你是资深中文科技与商业内容编辑，擅长从多篇资料中提炼有依据的深度文章。"
    )
    system = (
        f"{style_prompt}\n\n{ANTI_SLOP_SYSTEM_INSTRUCTION}\n\n"
        "所有事实只能来自编号参考资料。关键数据、判断和引语后必须标注 [n]，"
        "n 是支持该事实的资料编号。不同来源观点冲突时要明确说明，禁止自行补全。"
    )
    output_language = {
        "zh": "简体中文",
        "en": "English",
        "bilingual": "中英双语（先中文后英文，两版使用相同的资料编号）",
    }.get(lang, "简体中文")
    prompt = (
        f"围绕主题「{topic}」综合下面的多篇资料，写成一篇完整的新文章。"
        "不要逐篇摘要，要按论点组织并交叉印证。\n\n"
        f"输出语言：{output_language}。\n\n"
        "只输出 JSON 对象，字段：\n"
        '- "title_candidates": 3 个不夸大的中文标题\n'
        '- "body_md": Markdown 正文，关键事实后使用 [1]、[2] 等引用标记\n'
        '- "citations": [{"claim":"正文中的关键事实",'
        '"source_indexes":[1,2],"quote":"可选的原文短句"}]\n'
        "不要输出代码围栏或额外说明。\n\n"
        f"参考资料：\n{_source_block(articles)}"
    )

    parsed = None
    raw = ""
    for attempt in range(2):
        retry = (
            "\n\n上一次输出无法解析。必须只输出有效 JSON，字符串中的换行用 \\n。"
            if attempt else ""
        )
        raw = provider.chat([
            Message(role="system", content=system),
            Message(role="user", content=prompt + retry),
        ])
        parsed = _extract_json(raw)
        if parsed and parsed.get("title_candidates") and parsed.get("body_md"):
            break
        fallback = _extract_json_fallback(raw)
        if fallback:
            parsed = fallback
            break
    if not parsed or not parsed.get("title_candidates") or not parsed.get("body_md"):
        raise ValueError("综合创作模型未返回有效 JSON")

    titles = [str(title).strip() for title in parsed["title_candidates"]
              if str(title).strip()][:3]
    if not titles:
        titles = [topic]
    body = post_process(str(parsed["body_md"]).strip())
    body = _link_citation_markers(body, articles)
    body = f"{body.rstrip()}\n\n---\n\n{_references(articles)}\n"
    citations = _coerce_citations(parsed.get("citations"), len(articles))
    flagged = fact_check_multi(body, articles, provider)
    return SynthesisResult(titles, body, citations, flagged)
