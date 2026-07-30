from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.llm.base import LLMProvider, Message
from app.models import Article
from app.pipeline.anti_slop import ANTI_SLOP_SYSTEM_INSTRUCTION, post_process
from app.pipeline.rewriter import (
    _extract_json, _extract_json_fallback, _normalise_tags)

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


def _style_guidance(style) -> str:
    """Extract reusable style rules without the single-source template shell."""
    if style is None:
        return ""
    analysis = getattr(style, "analysis", None)
    if isinstance(analysis, dict):
        rules = [
            f"{key}：{str(value).strip()}"
            for key, value in analysis.items() if str(value).strip()
        ]
        if rules:
            return "\n".join(rules)
    instruction = str(getattr(style, "instruction", "") or "")
    match = re.search(
        r"### 写作风格\s*\n(.*?)(?=\n### |\Z)",
        instruction,
        flags=re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def _style_examples(style) -> str:
    examples = list(getattr(style, "examples", []) or [])[:2] if style else []
    blocks: list[str] = []
    for index, example in enumerate(examples, 1):
        if not isinstance(example, dict):
            continue
        content = str(example.get("content") or "").strip()[:3000]
        if not content:
            continue
        title = str(example.get("title") or "").strip()
        heading = f"示例 {index}" + (f"：{title}" if title else "")
        blocks.append(f"#### {heading}\n{content}")
    if not blocks:
        return ""
    return (
        "### 目标风格示例\n"
        "以下片段只用于模仿表达方式、语气和结构。绝不能把示例中的"
        "事实、人物、数据或观点写入当前文章。\n\n"
        + "\n\n".join(blocks)
    )


def _seo_instruction(enabled: bool) -> str:
    if not enabled:
        return ""
    return (
        "### 文章标签\n"
        "在正文末尾、参考文献之前单独输出一行标签，格式固定为："
        "`**标签**：#关键词1 #关键词2 #关键词3`。"
        "从标题和正文提取 5-8 个具体关键词，覆盖核心主题、关键实体和"
        "行业术语；每个标签以 # 开头，标签内不要有空格，不得使用正文"
        "未提及的词，不要添加解释。"
    )


def _promotion_instruction(footer: str) -> str:
    value = (footer or "").strip()
    if not value:
        return ""
    return (
        "### 推广部分\n"
        "在标签之前写一段与本文主题自然衔接的推广引导，包含 2-3 个"
        "互动呼吁，并在末尾标注品牌。以下内容只用于参考品牌名称与结构，"
        "不得照抄其中的主题文案：\n"
        f"{value}"
    )


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
               *, style=None, lang: str = "zh",
               promotion_footer: str = "",
               seo_tags_enabled: bool = True) -> SynthesisResult:
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
    guidance = _style_guidance(style)
    examples = _style_examples(style)
    content_rules = "\n\n".join(filter(None, [
        f"### 写作风格\n{guidance}" if guidance else "",
        examples,
        _promotion_instruction(promotion_footer),
        _seo_instruction(seo_tags_enabled),
    ]))
    prompt = (
        f"围绕主题「{topic}」综合下面的多篇资料，写成一篇完整的新文章。"
        "不要逐篇摘要，要按论点组织并交叉印证。\n\n"
        f"输出语言：{output_language}。\n\n"
        f"{content_rules}\n\n"
        "只输出 JSON 对象，字段：\n"
        '- "title_candidates": 3 个不夸大的候选标题\n'
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
    body = _normalise_tags(body)
    body = _link_citation_markers(body, articles)
    body = f"{body.rstrip()}\n\n---\n\n{_references(articles)}\n"
    citations = _coerce_citations(parsed.get("citations"), len(articles))
    flagged = fact_check_multi(body, articles, provider)
    return SynthesisResult(titles, body, citations, flagged)
