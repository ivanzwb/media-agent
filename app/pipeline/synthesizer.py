from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.llm.base import LLMProvider, Message
from app.models import Article
from app.pipeline.anti_slop import ANTI_SLOP_SYSTEM_INSTRUCTION, post_process
from app.pipeline.localize import is_page_furniture
from app.pipeline.rewriter import (
    DIGEST_INSTRUCTION, KEYWORD_INSTRUCTION, OPENING_INSTRUCTION,
    TITLE_INSTRUCTION, _extract_json, _extract_json_fallback, _normalise_tags,
    clean_digest, rank_titles)

_SOURCE_CHARS = 2500
_TOTAL_SOURCE_CHARS = 18000
# 机制、参数、失败案例这些能写出深度的内容，通常在一篇技术长文的中后段。
# 按常规预算截断，深入档的稿子等于只看着别人的开头在写。
_DEEP_SOURCE_CHARS = 4000
_DEEP_TOTAL_SOURCE_CHARS = 24000
_MAX_REF_IMAGES = 12  # 参考资料配图清单上限，与 _MAX_IMG 对齐
_MEDIA_TOKEN_RE = re.compile(r"\[\[(?:IMG|VID|VIDEO):\d+\]\]")


@dataclass
class SynthesisResult:
    title_candidates: list[str]
    body_md: str
    citations: list[dict] = field(default_factory=list)
    flagged_claims: list[str] = field(default_factory=list)
    digest: str = ""
    title_scores: list[dict] = field(default_factory=list)


def _article_images(article: Article) -> list[str]:
    """The pictures worth offering the model, in the order they were found.

    An article's image list is the whole page's <img> tags, so the site's
    logos, share icons and QR codes come along with the photographs. Offering
    those invites the model to place one mid-article.
    """
    return [u for u in (article.images or [])
            if u.startswith(("http://", "https://"))
            and not is_page_furniture(u)][:_MAX_REF_IMAGES]


def _plain_source_text(content: str) -> str:
    """Drop the extractor's media tokens from source text.

    Archived bodies carry [[IMG:N]] markers numbered per article. Left in the
    prompt the model copies them into its own draft, where the same number
    means a different picture — or none at all.
    """
    return _MEDIA_TOKEN_RE.sub("", content)


def _source_block(articles: list[Article],
                  include_images: bool = False, *, deep: bool = False) -> str:
    per_source = _DEEP_SOURCE_CHARS if deep else _SOURCE_CHARS
    budget = _DEEP_TOTAL_SOURCE_CHARS if deep else _TOTAL_SOURCE_CHARS
    blocks: list[str] = []
    used = 0
    shown = 0  # how many images the manifest holds before this source
    for index, article in enumerate(articles, 1):
        remaining = budget - used
        if remaining <= 0:
            break
        content = _plain_source_text(
            article.content_md or article.raw_summary or "")
        content = content[:min(per_source, remaining)]
        used += len(content)
        block = (
            f"[{index}] 标题：{article.title}\n"
            f"来源：{article.source_name}\n"
            f"URL：{article.url}\n"
            f"正文：\n{content}"
        )
        imgs = _article_images(article)
        if include_images and imgs:
            # Hand the model the exact token to copy. Numbering the images per
            # source (图1、图2…) and then resolving them against one global
            # list meant the two numberings drifted apart as soon as a second
            # source had images.
            block += "\n配图（引用时照抄方括号里的标记）：\n" + "\n".join(
                f"- [[IMG:{shown + offset}]]（{u}）"
                for offset, u in enumerate(imgs))
        shown += len(imgs)
        blocks.append(block)
    return "\n\n---\n\n".join(blocks)


_IMG_REF_RE = re.compile(r"\[\[IMG:(\d+)\]\]")


def _image_manifest(articles: list[Article]) -> list[str]:
    """Ordered list of http(s) image URLs across articles.

    This is what [[IMG:N]] means, so the 配图 lists in the source block are
    numbered straight off this list — including its per-source cap, which the
    prompt used to apply and this list did not.
    """
    manifest: list[str] = []
    for article in articles:
        manifest.extend(_article_images(article))
    return manifest


def referenced_images(body_md: str, articles: list[Article]) -> list[str]:
    """Image URLs actually referenced by [[IMG:N]] placeholders in body_md."""
    manifest = _image_manifest(articles)
    refs: list[str] = []
    for m in _IMG_REF_RE.finditer(body_md):
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        if 0 <= n < len(manifest):
            url = manifest[n]
            if url not in refs:
                refs.append(url)
    return refs


def resolve_image_refs(body_md: str, manifest: list[str],
                       local_map: dict[str, str]
                       ) -> tuple[str, list[str], list[str]]:
    """Turn [[IMG:N]] into images, reporting what resolved and what did not.

    A placeholder we cannot resolve — the model made up the number, or the
    download failed — is dropped. Left in place it reaches the editor as a
    literal "[[IMG:0]]", which is worse than the missing picture.
    """
    resolved: list[str] = []
    dropped: list[str] = []

    def _repl(m: re.Match) -> str:
        n = int(m.group(1))
        url = manifest[n] if 0 <= n < len(manifest) else ""
        new = local_map.get(url, "") if url else ""
        if not new and url.startswith("/media/"):
            new = url  # already local, nothing to download
        if not new:
            dropped.append(m.group(0))
            return ""
        resolved.append(f"{m.group(0)} → {new}")
        return f"![]({new})"

    body = re.sub(r"\n{3,}", "\n\n", _IMG_REF_RE.sub(_repl, body_md))
    return body, resolved, dropped


def expand_image_refs(body_md: str, articles: list[Article],
                      local_map: dict[str, str]) -> str:
    """Replace [[IMG:N]] with ![](<local web path>) using an old→new URL map."""
    return resolve_image_refs(
        body_md, _image_manifest(articles), local_map)[0]


def _visual_instruction(has_images: bool) -> str:
    parts = [
        "### 可视化（按需）",
        "如果适合，正文可包含：",
        "- Mermaid 流程图：用 ```mermaid 代码围栏包裹，如 flowchart 或"
        " sequenceDiagram，节点文本用简短中文，避免复杂子图语法，确保围栏闭合。",
    ]
    if has_images:
        parts.append(
            "- 引用资料配图：在正文需要配图的位置单独成行，照抄参考资料「配图」"
            "清单里给出的 [[IMG:N]] 标记，编号必须与清单一致，不要自己编。"
            "清单里没有的编号一律不要写。"
            "只引用与上下文相关的图，不要为凑数而引用。"
        )
    parts.append("没有合适的图或图无助于表达时，不要强行插入。")
    return "\n".join(parts)


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


_EXTERNAL_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(https?://[^)\s]+\)")


def _strip_external_links(body: str) -> str:
    """Keep the anchor text, drop the URL.

    公众号、头条等平台把站外链接判为引流并降权，所以正文不能带任何外链。
    图片语法和本地 media 相对路径不受影响。
    """
    return _EXTERNAL_LINK_RE.sub(r"\1", body)


def _references(articles: list[Article]) -> str:
    lines = ["## 参考文献"]
    for index, article in enumerate(articles, 1):
        lines.append(f"{index}. {article.title} — {article.source_name}")
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


DEPTHS = ("beginner", "intermediate", "advanced")

# Length is a range, not a floor. Third-party reviews put the completion rate
# of articles past 3000 characters below 30% — the line under which the feed
# stops recommending — so a floor of 3500 was aiming past the cliff. Depth has
# to come out of information density instead.
_DEPTH_GUIDANCE = {
    "beginner": (
        "面向零基础读者。每个概念都要有一句大白话解释和一个贴近日常经验的类比；"
        "推导和实现细节可以略过，但“它是什么、为什么需要它、没有它会怎样”"
        "必须讲清楚。正文 1200-1800 字（英文 700-1100 词）。"
    ),
    "intermediate": (
        "面向有基础的读者。默认读者认得这个领域最基本的名词，重点讲原理，"
        "以及几种做法之间的差别与取舍：谁在什么条件下更合适、代价是什么。"
        "正文 1500-2200 字（英文 900-1300 词）。"
    ),
    "advanced": (
        "面向从业者。要写到能动手的层面：机制怎么运转、关键参数和常见取值、"
        "典型的失败模式、与相近方案的具体差异、目前公认的局限和尚未解决的问题。"
        "需要时可以出现公式、伪代码、配置项和源码级细节。"
        "正文 2000-2800 字（英文 1200-1700 词）。"
    ),
}


def _depth_instruction(depth: str) -> str:
    """How deep to go, and what counts as deep.

    Without this the model writes the same middling overview whatever the
    series was positioned as — a page of correct-sounding terminology that
    leaves a reader knowing no more than the table of contents told them.
    """
    level = _DEPTH_GUIDANCE.get(depth)
    if not level:
        return ""
    return (
        "### 内容深度\n"
        f"{level}\n"
        "以下几条不分档位，都必须做到：\n"
        "- 术语、模型名、方法名第一次出现时，先用一句话说清它是什么、"
        "解决什么问题，再往下用。只报名字不解释，等于没写。\n"
        "- 讲到一个方法就要讲到机制：它靠什么做到的、比原来的做法好在哪、"
        "代价和适用边界是什么。不要停在“效果显著”“性能强大”这类结论上。\n"
        "- 资料里的数字、参数、前提条件、对比结果和具体例子要用上，"
        "并标注来源编号；能落到具体处，就不要停在概括。\n"
        "- 宁可少讲两个点，也要把讲到的点讲透。不要靠罗列名词凑覆盖面，"
        "那样读者读完只记住一串词。\n"
        "- 字数是区间不是目标：上限到了就删，不要靠复述、过渡句和小结把"
        "篇幅撑大。深度体现在信息密度，不在长度——写长了读者读不完，"
        "读不完的稿子平台不会再推。"
    )


def _brief_instruction(scope: str) -> str:
    value = (scope or "").strip()
    if not value:
        return ""
    return (
        "### 本篇要交付什么\n"
        "这是提纲对本篇的要求，正文必须把它兑现，"
        "不要写成同一主题下的又一篇泛泛综述：\n"
        f"{value}"
    )


@dataclass
class SeriesPlacement:
    """Where an article sits in a series, so the seams can be written.

    Chapters reach a reader one at a time, days apart. What makes them read as
    a series rather than as loose articles on one subject is each chapter
    picking up where the last one stopped and saying what the next one answers.
    """
    scope: str = ""    # what the outline promised this chapter would deliver
    behind: str = ""   # chapters already written, by title and opening line
    ahead: str = ""    # the next chapter, by title and scope
    outline: str = ""  # the whole plan, for the opening chapter to introduce
    closing: bool = False  # nothing follows: land the series instead


def _series_context_instruction(behind: str) -> str:
    value = (behind or "").strip()
    if not value:
        return ""
    return (
        "### 系列上下文\n"
        "本文是一个系列中的一篇。以下是已写完的前置章节，用于承接行文、"
        "避免重复展开讲过的内容。开头要顺着上一篇的结尾往下写，"
        "不要从零重新介绍这个主题。它们不是参考资料，"
        "不得作为事实来源引用：\n"
        f"{value}"
    )


def _series_handoff_instruction(placement: SeriesPlacement) -> str:
    ahead = placement.ahead.strip()
    if ahead:
        return (
            "### 交给下一篇\n"
            "正文收尾处（若有推广与标签，放在它们之前）留一小段把读者带到"
            "下一篇：点出本篇讲完之后新冒出来的那个问题，"
            "说明它由下一篇回答，并写出下一篇的标题。这个钩子必须落在下一篇"
            "真实要讲的内容上，不要写“敬请期待”“欲知后事如何”这类空话，"
            "也不要许诺下一篇并不打算讲的东西。下一篇是：\n"
            f"{ahead}"
        )
    if placement.closing:
        return (
            "### 收束整个系列\n"
            "本文是这个系列的最后一篇。正文收尾处（若有推广与标签，放在它们"
            "之前）要收住整个系列：回到开篇提出的问题，"
            "交代读者读完这一路应该带走什么判断，不要再预告新的内容。"
        )
    return ""


def _series_opening_instruction(outline: str) -> str:
    value = (outline or "").strip()
    if not value:
        return ""
    return (
        "### 系列开篇\n"
        "本文是这个系列的第一篇，读者从这里进入整个主题。除了写好本篇自己的"
        "内容，还要用一节交代全局：这个主题由哪几块组成、它们之间是什么关系、"
        "后面每一篇分别解决什么问题、建议按什么顺序读。各章的细节留给它们自己"
        "展开，这里只提纲挈领。下面是系列的章节安排与知识脉络，它们不是参考"
        "资料，不得作为事实来源引用，也不要照抄章节描述的措辞：\n"
        f"{value}"
    )


def synthesize(topic: str, articles: list[Article], provider: LLMProvider,
               *, style=None, lang: str = "zh",
               promotion_footer: str = "",
               seo_tags_enabled: bool = True,
               depth: str = "",
               series: SeriesPlacement | None = None,
               include_images: bool = True) -> SynthesisResult:
    if len(articles) < 2:
        raise ValueError("多源综合至少需要 2 篇有效参考资料")

    placement = series or SeriesPlacement()
    is_deep = depth == "advanced"
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
    has_images = include_images and any(
        u.startswith(("http://", "https://")) for a in articles
        for u in (a.images or []))
    content_rules = "\n\n".join(filter(None, [
        f"### 写作风格\n{guidance}" if guidance else "",
        examples,
        _depth_instruction(depth),
        OPENING_INSTRUCTION.strip(),
        _brief_instruction(placement.scope),
        _series_context_instruction(placement.behind),
        _series_opening_instruction(placement.outline),
        _series_handoff_instruction(placement),
        _visual_instruction(has_images),
        _promotion_instruction(promotion_footer),
        _seo_instruction(seo_tags_enabled),
        KEYWORD_INSTRUCTION.strip(),
        TITLE_INSTRUCTION.strip(),
        DIGEST_INSTRUCTION.strip(),
    ]))
    prompt = (
        f"围绕主题「{topic}」综合下面的多篇资料，写成一篇完整的新文章。"
        "不要逐篇摘要，要按论点组织并交叉印证。\n\n"
        f"输出语言：{output_language}。\n\n"
        f"{content_rules}\n\n"
        "只输出 JSON 对象，字段：\n"
        '- "title_candidates": 5 个候选标题，按你的排序，最好的放第一个\n'
        '- "title_scores": [{"title":"候选原文","score":0-100 的总分,'
        '"reason":"一句话说明"}]\n'
        '- "digest": 50-60 字摘要\n'
        '- "body_md": Markdown 正文，关键事实后使用 [1]、[2] 等引用标记；'
        "正文中不要出现任何链接或网址；"
        "Mermaid 流程图必须用 ```mermaid 代码围栏包裹\n"
        '- "citations": [{"claim":"正文中的关键事实",'
        '"source_indexes":[1,2],"quote":"可选的原文短句"}]\n'
        "不要输出代码围栏或额外说明。\n\n"
        "参考资料：\n"
        f"{_source_block(articles, include_images=has_images, deep=is_deep)}"
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

    titles, title_scores = rank_titles(parsed["title_candidates"],
                                       parsed.get("title_scores"))
    if not titles:
        titles = [topic]
        title_scores = []
    body = post_process(str(parsed["body_md"]).strip())
    body = _normalise_tags(body)
    body = _strip_external_links(body)
    body = f"{body.rstrip()}\n\n---\n\n{_references(articles)}\n"
    citations = _coerce_citations(parsed.get("citations"), len(articles))
    flagged = fact_check_multi(body, articles, provider)
    return SynthesisResult(titles, body, citations, flagged,
                           clean_digest(str(parsed.get("digest") or "")),
                           title_scores)
