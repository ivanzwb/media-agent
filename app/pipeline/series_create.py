"""Knowledge series creation: one topic in, a structured set of drafts out.

Where search creation answers "write me a piece about X", this answers "walk
me through X in order". The shape of the run differs in two ways that drive
the whole design:

* It is long. A five-part series is five research-and-write cycles, so chapter
  rows carry the progress and a starved chapter is isolated rather than fatal.
* The outline decides everything downstream. The chapter search terms it emits
  determine whether each chapter can find sources at all, so the outline is
  grounded in a cheap search pass instead of the model's memory alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import re
from typing import Callable
from urllib.parse import urlsplit

from app.llm.base import LLMProvider, Message
from app.models import Article, Draft
from app.config import Config
from app.pipeline.rewriter import _extract_json
from app.pipeline.sanitizer import sanitize_draft
from app.pipeline.search_create import (
    ProgressCallback, SearchCreateCancelled, SearchCreateOptions, _check_cancel,
    _emit, _topic_search_terms, _url_key, collect_references, search_queries)
from app.pipeline.localize import localize_reference_images
from app.pipeline.synthesizer import (
    SeriesPlacement, expand_image_refs, referenced_images, synthesize)
from app.sources.web_search import DEFAULT_SEARCH_ENGINES, SearchHit

logger = logging.getLogger(__name__)

MIN_PARTS = 3
MAX_PARTS = 10
DEPTHS = ("beginner", "intermediate", "advanced")

# Depth has to reach the search terms too: an outline that asks for
# introductions collects introductions, and no writing prompt can make a
# chapter deeper than the material it was given.
_DEPTH_QUERY_HINTS = {
    "beginner": "检索词偏向入门、科普、图解一类的资料。",
    "intermediate": "检索词偏向原理讲解、方案对比一类的资料。",
    "advanced": "检索词要能搜到原理、实现、论文、文档、源码一类的资料，"
                "而不是入门介绍。",
}

_DEPTH_LABELS = {
    "beginner": "入门：面向零基础读者，重解释、类比和直觉",
    "intermediate": "进阶：面向有基础的读者，重原理、对比和取舍",
    "advanced": "深入：面向从业者，重实现细节、前沿进展和局限",
}

# Grounding is titles and snippets only, so these caps are about keeping the
# outline prompt focused rather than about cost.
_GROUNDING_CHARS = 6000
_GROUNDING_PER_DOMAIN = 2
_SNIPPET_CHARS = 180
_SUMMARY_CHARS = 200
_MAP_CHARS = 2000
# Search hits shrink a lot on the way to usable references — pages fail to
# fetch, get dropped as marketing or near-duplicates — so clearing the
# two-reference bar needs noticeably more than two hits.
_PREFLIGHT_MIN_HITS = 4


@dataclass
class SeriesCreateOptions:
    topic: str
    lang: str = "zh"
    depth: str = "intermediate"
    # None asks the outline to decide how many chapters the subject needs.
    parts: int | None = None
    style_id: str | None = None
    engines: tuple[str, ...] | list[str] = DEFAULT_SEARCH_ENGINES
    ref_count: int = 5
    workers: int = 6
    proxy: str | None = None

    def chapter_options(self) -> SearchCreateOptions:
        """Search options for one chapter.

        A knowledge series covers settled material, so the 30-day window that
        suits single-draft creation would exclude exactly the explanatory
        writing these chapters need.
        """
        return SearchCreateOptions(
            topic=self.topic, lang=self.lang, time_range_days=None,
            ref_count=self.ref_count, style_id=self.style_id,
            engines=self.engines, workers=self.workers, proxy=self.proxy)

    def validate(self) -> None:
        self.topic = (self.topic or "").strip()
        if not self.topic:
            raise ValueError("请输入系列主题")
        if len(self.topic) > 200:
            raise ValueError("系列主题不能超过 200 个字符")
        if self.lang not in ("zh", "en", "bilingual"):
            raise ValueError("语言必须为 zh、en 或 bilingual")
        if self.depth not in DEPTHS:
            raise ValueError("深度必须为 beginner、intermediate 或 advanced")
        if self.parts is not None:
            try:
                self.parts = int(self.parts)
            except (TypeError, ValueError):
                raise ValueError("章节数必须为整数") from None
            if not MIN_PARTS <= self.parts <= MAX_PARTS:
                raise ValueError(f"章节数必须在 {MIN_PARTS}-{MAX_PARTS} 之间")
        # Engine, ref_count and worker rules are identical to single-draft
        # creation; validating a derived instance keeps them in one place.
        checked = self.chapter_options()
        checked.validate()
        self.engines = checked.engines
        self.workers = checked.workers


def probe_topic(options: SeriesCreateOptions, *,
                progress: ProgressCallback | None = None,
                should_stop: Callable[[], bool] | None = None,
                stats: dict | None = None) -> str:
    """Collect titles and snippets to ground the outline. No page fetching.

    Fetching is what costs time in this pipeline; search results already carry
    a title and snippet, which is all the outline needs to pick terminology
    that matches what is actually published.
    """
    _check_cancel(should_stop)
    _emit(progress, "probe", "正在检索该主题的公开资料…", stats=stats)
    search_options = options.chapter_options()
    search_options.validate()
    base = _topic_search_terms(options.topic, options.lang)
    if options.lang == "en":
        queries = [base, f"{base} overview", f"{base} tutorial"]
    else:
        queries = [base, f"{base} 综述", f"{base} 入门"]

    hits = search_queries(queries, search_options, should_stop=should_stop)
    lines: list[str] = []
    per_domain: dict[str, int] = {}
    used = 0
    for hit in hits:
        domain = urlsplit(hit.url).netloc.lower().removeprefix("www.")
        # One content farm publishing ten near-identical pages would otherwise
        # dominate the sample and skew the outline towards its framing.
        if per_domain.get(domain, 0) >= _GROUNDING_PER_DOMAIN:
            continue
        snippet = " ".join((hit.snippet or "").split())[:_SNIPPET_CHARS]
        line = f"- {hit.title}" + (f"：{snippet}" if snippet else "")
        if used + len(line) > _GROUNDING_CHARS:
            break
        per_domain[domain] = per_domain.get(domain, 0) + 1
        lines.append(line)
        used += len(line)

    if stats is not None:
        stats["grounding_hits"] = len(lines)
    _emit(progress, "probe", f"已采集 {len(lines)} 条资料标题用于校准提纲",
          stats=stats)
    return "\n".join(lines)


_MERMAID_HEADER = re.compile(r"^(?:graph|flowchart)\b", re.IGNORECASE)
_MERMAID_ARROW = re.compile(r"\s*(?:-{2,}>|={2,}>|-{3,}|\.{2,}>)\s*")
_MERMAID_SIDE = re.compile(
    r'^(?P<id>[^\[\](){}"|]+?)\s*'
    r'(?:\[\s*"?(?P<square>[^\]"]*)"?\s*\]'
    r'|\(\s*"?(?P<round>[^)"]*)"?\s*\)'
    r'|\{\s*"?(?P<brace>[^}"]*)"?\s*\})?$')
_MERMAID_UNSAFE = re.compile(r'["\[\]{}()<>|;`#]+')
_MAP_NODE_CHARS = 40


def _mermaid_label(text: str) -> str:
    return " ".join(_MERMAID_UNSAFE.sub(" ", text or "").split())[:_MAP_NODE_CHARS]


def _clean_mermaid(raw: str) -> str:
    """Rebuild the diagram from the parts that are certainly valid.

    A diagram the model writes freehand fails on punctuation the renderer
    treats as syntax, and a broken diagram is worse than none. So the text is
    read for nodes and edges and written out again in one canonical form,
    with generated ids: whatever the model called its nodes cannot break it.
    """
    ids: dict[str, str] = {}
    labels: dict[str, str] = {}
    edges: list[tuple[str, str]] = []

    def node(side: str) -> str | None:
        match = _MERMAID_SIDE.match(side.strip())
        if not match:
            return None
        origin = (match.group("id") or "").strip()
        written = (match.group("square") or match.group("round")
                   or match.group("brace"))
        label = _mermaid_label(written if written is not None else origin)
        if not origin or not label:
            return None
        key = ids.setdefault(origin, f"N{len(ids) + 1}")
        if written is not None:
            labels[key] = label
        else:
            labels.setdefault(key, label)
        return key

    for line in str(raw or "").splitlines():
        text = line.strip().rstrip(";")
        if not text or text.startswith("```") or _MERMAID_HEADER.match(text):
            continue
        # Edge captions break more often than they explain.
        text = re.sub(r"\|[^|]*\|", "", text)
        sides = _MERMAID_ARROW.split(text)
        if len(sides) >= 2:
            chain = [node(side) for side in sides]
            edges += [(left, right) for left, right in zip(chain, chain[1:])
                      if left and right and left != right]
        elif "[" in text:
            node(text)

    # A node nothing connects to came from a stray line of prose, not the map.
    linked = {key for edge in edges for key in edge}
    if len(linked) < 2:
        return ""
    lines = ["graph TD"]
    lines += [f'    {key}["{label}"]' for key, label in labels.items()
              if key in linked]
    seen: set[tuple[str, str]] = set()
    for edge in edges:
        if edge not in seen:
            seen.add(edge)
            lines.append(f"    {edge[0]} --> {edge[1]}")
    text = ""
    for line in lines:
        # Truncating mid-line would leave a diagram that cannot be parsed.
        if len(text) + len(line) + 1 > _MAP_CHARS:
            break
        text += line + "\n"
    return text.rstrip()


def generate_knowledge_map(topic: str, grounding: str, provider: LLMProvider,
                           *, depth: str, lang: str = "zh") -> str:
    """Sketch the shape of the field as a diagram, before the outline exists.

    Written first so the chapter split follows the subject's own structure
    rather than a generic "background, principles, practice" arc. A graph says
    which ideas hang off which more plainly than prose, and it doubles as the
    reader-facing picture of what the series covers.
    """
    language = {"zh": "中文", "en": "English",
                "bilingual": "中英双语"}.get(lang, "中文")
    grounding_block = (
        f"该主题公开资料的标题与摘要，用于校准术语：\n{grounding}\n\n"
        if grounding.strip() else "")
    prompt = (
        f"梳理知识主题「{topic}」的核心脉络，画成一张 Mermaid 图。"
        f"深度定位：{_DEPTH_LABELS[depth]}。输出语言：{language}。\n\n"
        f"{grounding_block}"
        "第一行写 graph TD，其后每行一条边，形如：\n"
        'A["根主题"] --> B["主要分支"]\n'
        "要求：8-16 个节点，最多三层；根节点是该主题本身，第二层是主要分支，"
        "第三层是分支下的关键概念或方法。\n"
        "节点文字不超过 12 字，且不得包含括号、引号、分号。"
        "不要使用 mindmap、subgraph、classDef、style，不要输出代码围栏或说明文字。"
    )
    try:
        return _clean_mermaid(
            provider.chat([Message(role="user", content=prompt)]))
    except Exception as exc:  # noqa: BLE001
        # The map is for orientation, not correctness; losing it must not stop
        # a run the user already paid the grounding search for.
        logger.warning("知识架构生成失败：%s", exc)
        return ""


def _normalise_chapters(value, topic: str, parts: int,
                        lang: str) -> list[dict]:
    if not isinstance(value, list):
        return []
    chapters: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        raw_queries = item.get("search_queries") or item.get("queries") or []
        if not isinstance(raw_queries, list):
            raw_queries = [raw_queries]
        queries = [str(query).strip() for query in raw_queries
                   if str(query).strip()][:4]
        if not queries:
            queries = [f"{_topic_search_terms(topic, lang)} {title}".strip()]
        raw_prereq = item.get("prerequisites") or []
        if not isinstance(raw_prereq, list):
            raw_prereq = [raw_prereq]
        chapters.append({
            "title": title,
            "scope": str(item.get("scope") or "").strip(),
            "search_queries": queries,
            "prerequisites": [str(name).strip() for name in raw_prereq
                              if str(name).strip()],
        })
        if len(chapters) >= parts:
            break
    return chapters


def generate_series_outline(topic: str, grounding: str, provider: LLMProvider,
                            *, parts: int | None = None, depth: str,
                            lang: str = "zh",
                            knowledge_map: str = "") -> list[dict]:
    """Split the subject into chapters.

    How many chapters is part of the answer, not part of the question: a
    number picked before anyone has looked at the field makes the model pad a
    narrow subject or compress a broad one. Passing `parts` forces a count for
    the cases where the user wants one.
    """
    language = {"zh": "中文", "en": "English",
                "bilingual": "中英双语"}.get(lang, "中文")
    grounding_block = (
        "下面是该主题公开资料的标题与摘要，只用于校准术语用词和覆盖面，"
        f"不要当作事实来源，也不要照抄：\n{grounding}\n\n"
        if grounding.strip() else ""
    )
    map_block = (
        "该领域的核心脉络如下（Mermaid 图，A --> B 表示 B 是 A 的下一层），"
        f"章节划分应覆盖其中的主要分支：\n{knowledge_map}\n\n"
        if knowledge_map.strip() else ""
    )
    if parts:
        opening = f"为知识主题「{topic}」设计一个 {parts} 篇的系列文章提纲。\n"
        count_rule = f"chapters 必须恰好 {parts} 项。"
    else:
        opening = (
            f"为知识主题「{topic}」设计一个系列文章提纲。\n"
            "篇数由主题本身决定：先看讲清楚这个领域需要哪几块，再定几章，"
            f"取值 {MIN_PARTS}-{MAX_PARTS}（含开篇总览）。主题窄、主线少就少写几章；"
            "分支多、每块都撑得起一篇才多写。深度定位也决定粒度："
            "入门可以把细节并进一章，深入才值得为一个分支单开一章。\n")
        count_rule = (
            f"chapters 为 {MIN_PARTS}-{MAX_PARTS} 项，"
            "宁可少而扎实，也不要为凑数拆出内容重复或撑不满一篇的章节。")
    prompt = (
        f"{opening}"
        f"深度定位：{_DEPTH_LABELS[depth]}。输出语言：{language}。\n"
        "章节之间要有清晰的递进关系，共同覆盖该领域的主要脉络，"
        "彼此不重复。\n"
        "第一章是整个系列的开篇总览：交代这个主题的全貌、几个部分之间的关系，"
        "以及后面每一章各自解决什么问题，本身不深入某一个分支；"
        "它的检索词用该领域综述、入门一类的说法。\n\n"
        f"{map_block}{grounding_block}"
        "只输出 JSON 对象：\n"
        '{"chapters":[{"title":"章节标题",'
        '"scope":"这一章讲什么、读者读完能得到什么，100 字以内",'
        '"search_queries":["2-4 个用于检索该章资料的检索词，短而具体，'
        '使用该领域实际通用的说法"],'
        '"prerequisites":["需要先读的本系列其他章节标题"]}]}\n'
        f"{_DEPTH_QUERY_HINTS[depth]}\n"
        f"{count_rule}不要输出代码围栏或额外说明。"
    )
    raw = provider.chat([Message(role="user", content=prompt)])
    chapters = _normalise_chapters(
        (_extract_json(raw) or {}).get("chapters"), topic,
        parts or MAX_PARTS, lang)
    if not chapters:
        logger.warning("系列提纲解析失败，响应：%r", str(raw)[:300])
        raise ValueError("系列提纲生成失败，请调整主题后重试")
    return chapters


def _repair_chapter_queries(chapters: list[dict], thin: list[int], topic: str,
                            grounding: str,
                            provider: LLMProvider) -> dict[int, list[str]]:
    listing = "\n".join(
        f"{index}. {chapters[index - 1]['title']}"
        f"（现检索词：{'、'.join(chapters[index - 1]['search_queries'])}）"
        for index in thin)
    grounding_block = (
        f"该主题公开资料的标题与摘要，请从中借用实际的说法：\n{grounding}\n\n"
        if grounding.strip() else "")
    prompt = (
        f"系列主题「{topic}」的下列章节，其检索词在搜索引擎上几乎搜不到资料，"
        "请为每一章改写检索词。\n\n"
        f"{grounding_block}"
        f"需要改写的章节：\n{listing}\n\n"
        '只输出 JSON：{"fixes":[{"index":1,"search_queries":["检索词"]}]}\n'
        "检索词要短、具体、用该领域公开资料里实际出现的说法，每章 2-4 个。"
    )
    try:
        parsed = _extract_json(
            provider.chat([Message(role="user", content=prompt)])) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("检索词修复调用失败：%s", exc)
        return {}
    if not isinstance(parsed, dict):
        return {}
    fixes: dict[int, list[str]] = {}
    for item in parsed.get("fixes") or []:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        queries = [str(query).strip()
                   for query in (item.get("search_queries") or [])
                   if str(query).strip()][:4]
        if index in thin and queries:
            fixes[index] = queries
    return fixes


def preflight_chapters(chapters: list[dict], options: SeriesCreateOptions,
                       provider: LLMProvider, *, grounding: str = "",
                       progress: ProgressCallback | None = None,
                       should_stop: Callable[[], bool] | None = None,
                       stats: dict | None = None
                       ) -> tuple[list[dict], dict[int, list[SearchHit]]]:
    """Check every chapter can find sources, before committing to the run.

    Search-only, like grounding. A chapter whose terms find almost nothing
    will fail twenty minutes from now for a reason that is fixable right now,
    while the outline is still cheap to change. The hits are kept and handed
    to the research stage, so most of what this costs comes back.
    """
    search_options = options.chapter_options()
    search_options.validate()
    total = len(chapters)
    seeds: dict[int, list[SearchHit]] = {}
    thin: list[int] = []

    for index, chapter in enumerate(chapters, 1):
        _check_cancel(should_stop)
        _emit(progress, "preflight",
              f"预检第 {index}/{total} 章《{chapter['title']}》…",
              current=index, total=total, stats=stats)
        hits = search_queries(
            chapter["search_queries"], search_options, should_stop=should_stop)
        seeds[index] = hits
        if len(hits) < _PREFLIGHT_MIN_HITS:
            thin.append(index)

    if thin:
        _check_cancel(should_stop)
        _emit(progress, "preflight",
              f"{len(thin)} 章资料偏少，正在改写检索词…",
              current=total, total=total, stats=stats)
        for index, queries in _repair_chapter_queries(
                chapters, thin, options.topic, grounding, provider).items():
            _check_cancel(should_stop)
            retried = search_queries(
                queries, search_options, should_stop=should_stop)
            if len(retried) > len(seeds[index]):
                chapters[index - 1]["search_queries"] = queries
                seeds[index] = retried

    for index, chapter in enumerate(chapters, 1):
        chapter["preflight_hits"] = len(seeds.get(index, []))
    starved = [chapters[index - 1]["title"] for index in range(1, total + 1)
               if len(seeds[index]) < _PREFLIGHT_MIN_HITS]
    if stats is not None:
        stats["preflight_thin"] = starved
    _emit(progress, "preflight",
          f"预检完成：{total - len(starved)}/{total} 章资料充足"
          + (f"，{'、'.join(starved)} 可能写不成" if starved else ""),
          current=total, total=total, stats=stats)
    return chapters, seeds


def _part_label(index: int, lang: str) -> str:
    return f"Part {index}" if lang == "en" else f"第 {index} 章"


def _chapter_summary(body_md: str) -> str:
    """First real paragraph, used as continuity context for later chapters.

    Deliberately derived from the text instead of asking the model for a
    summary: an extra call per chapter would scale the cost with series length
    for something this cheap to approximate.
    """
    for block in re.split(r"\n{2,}", body_md or ""):
        lines = [line for line in block.splitlines()
                 if line.strip()
                 and not line.lstrip().startswith(("#", "![", ">", "|", "---"))]
        text = re.sub(r"\[\d+\]", "", " ".join(lines))
        text = " ".join(text.split())
        if text:
            return text[:_SUMMARY_CHARS]
    return ""


def _series_nav_note(topic: str, index: int, titles: list[str],
                     prerequisites: list[str], lang: str) -> str:
    """A reader's way into and out of one chapter of a series.

    Published chapters land in a reader's feed one at a time, with nothing to
    say they belong together. Built from the chapter rows rather than asked of
    the model, so it cannot name a chapter the series does not have.
    """
    total = len(titles)
    if total < 2 or not 1 <= index <= total:
        return ""
    english = lang == "en"
    previous = titles[index - 2] if index > 1 else ""
    following = titles[index] if index < total else ""

    lines = [f"Part {index} of {total} in the \"{topic}\" series."
             if english else f"本文是《{topic}》系列第 {index} 篇，共 {total} 篇。"]
    around = []
    if previous:
        around.append(f"Previous: \"{previous}\"" if english
                      else f"上一篇《{previous}》")
    if following:
        around.append(f"Next: \"{following}\"" if english
                      else f"下一篇《{following}》")
    if around:
        lines.append(" | ".join(around) if english else "　".join(around))

    # A prerequisite the series covers itself is worth pointing at by number.
    earlier = {title: order for order, title in enumerate(titles[:index - 1], 1)}
    read_first = [
        (f"Part {earlier[item]} \"{item}\"" if english
         else f"第 {earlier[item]} 篇《{item}》") if item in earlier else item
        for item in prerequisites if item]
    if read_first:
        lines.append(("Read first: " if english else "建议先读：")
                     + ("; ".join(read_first) if english
                        else "、".join(read_first)))
    return "".join(f"> {line}\n" for line in lines) + "\n"


def _chapter_line(item: dict, lang: str) -> str:
    label = f"{_part_label(item['order'], lang)}《{item['title']}》"
    scope = (item.get("scope") or "").strip()
    return f"{label}：{scope}" if scope else label


def _series_roadmap(plan: list[dict], knowledge_map: str, lang: str) -> str:
    """The whole plan, written out for the opening chapter to introduce.

    The opener is the one chapter that has to speak for the series, and by the
    time it is written the outline is settled — so it describes chapters that
    will exist, in the order the reader will meet them.
    """
    text = "\n".join(_chapter_line(item, lang) for item in plan)
    diagram = (knowledge_map or "").strip()
    if diagram:
        text += (
            "\n\n该主题的知识脉络（Mermaid，A --> B 表示 B 是 A 的下一层）。"
            "若有助于读者建立全局印象，可以在开篇原样放进一个 mermaid 代码块，"
            f"不要改写其中的节点文字：\n{diagram}")
    return text


def _next_chapter_note(plan: list[dict], index: int, lang: str) -> str:
    """The chapter this one hands the reader to, if there is one."""
    following = next(
        (item for item in plan if item["order"] == index + 1), None)
    return _chapter_line(following, lang) if following else ""


def _chapter_context(written: list[dict], lang: str) -> str:
    return "\n".join(
        f"{_part_label(item['order'], lang)}《{item['title']}》：{item['summary']}"
        for item in written if item.get("summary"))


def _chapter_references(chapter: dict, search_options: SearchCreateOptions,
                        provider: LLMProvider, *, topic: str,
                        seen_urls: set[str],
                        hits: list[SearchHit] | None = None,
                        progress: ProgressCallback | None = None,
                        should_stop: Callable[[], bool] | None = None,
                        stats: dict | None = None) -> list[Article]:
    """References for one chapter, widening the query once if it comes up short.

    A narrow chapter — a glossary, a niche variant — is normal in a series, and
    the outline's terms for it are the most likely to miss. Retrying with the
    series topic prepended recovers most of those before the chapter is written
    off.
    """
    articles = collect_references(
        chapter["search_queries"], search_options, provider,
        topic=chapter["title"], seen_urls=seen_urls, hits=hits,
        progress=progress, should_stop=should_stop, stats=stats)
    widened = (f"{_topic_search_terms(topic, search_options.lang)} "
               f"{chapter['title']}").strip()
    if len(articles) >= 2 or widened in chapter["search_queries"]:
        return articles

    _emit(progress, "research",
          f"《{chapter['title']}》资料不足，放宽检索词重试…", stats=stats)
    keys = {_url_key(article.url) for article in articles}
    merged = list(articles)
    for extra in collect_references(
            [widened], search_options, provider, topic=chapter["title"],
            seen_urls=seen_urls | keys, progress=progress,
            should_stop=should_stop, stats=stats):
        key = _url_key(extra.url)
        if key not in keys:
            keys.add(key)
            merged.append(extra)
    return merged[:search_options.ref_count]


@dataclass
class _ChapterContext:
    """Everything a chapter needs that does not change between chapters."""
    store: object
    provider: LLMProvider
    options: SeriesCreateOptions
    search_options: SearchCreateOptions
    series_id: int
    style: object = None
    promotion_footer: str = ""
    seo_tags_enabled: bool = True
    sensitive_words: set[str] = field(default_factory=set)
    # Every chapter of the series, in order: order, title and scope.
    plan: list[dict] = field(default_factory=list)
    knowledge_map: str = ""
    config: Config | None = None

    def titles(self) -> list[str]:
        return [item["title"] for item in self.plan]


def _write_chapter(context: _ChapterContext, chapter: dict, chapter_id: int, *,
                   index: int, total: int, seen_urls: set[str],
                   written: list[dict],
                   hits: list[SearchHit] | None = None,
                   progress: ProgressCallback | None = None,
                   should_stop: Callable[[], bool] | None = None,
                   stats: dict | None = None) -> tuple[int, str]:
    """Research and write one chapter. Returns its draft id and summary.

    Shared by the full run and by rerunning a single chapter, so a rerun
    produces a chapter indistinguishable from one the original run wrote.
    """
    store = context.store
    options = context.options
    title = chapter["title"]

    _check_cancel(should_stop)
    store.update_chapter(chapter_id, status="researching", error=None)
    _emit(progress, "research",
          f"第 {index}/{total} 章《{title}》：正在检索资料…",
          current=index, total=total, stats=stats)
    articles = _chapter_references(
        chapter, context.search_options, context.provider, topic=options.topic,
        seen_urls=seen_urls, hits=hits, progress=progress,
        should_stop=should_stop, stats=stats)
    if len(articles) < 2:
        raise ValueError("有效参考资料不足 2 篇")

    saved_articles: list[Article] = []
    for article in articles:
        _check_cancel(should_stop)
        article.topic = options.topic
        saved_articles.append(store.save_article(article))
    seen_urls.update(_url_key(article.url) for article in articles)

    store.update_chapter(chapter_id, status="writing")
    _emit(progress, "write", f"第 {index}/{total} 章《{title}》：正在写作…",
          current=index, total=total, stats=stats)
    result = synthesize(
        f"{options.topic}：{title}", saved_articles, context.provider,
        style=context.style, lang=options.lang,
        promotion_footer=context.promotion_footer,
        seo_tags_enabled=context.seo_tags_enabled,
        # The depth the series was positioned at decides how the chapter is
        # written, not just how the outline was cut.
        depth=options.depth,
        series=SeriesPlacement(
            scope=chapter["scope"],
            # Only what the reader has read by this point in the series.
            behind=_chapter_context(
                [item for item in written if item["order"] < index],
                options.lang),
            ahead=_next_chapter_note(context.plan, index, options.lang),
            # The opening chapter carries the series: it is where a reader
            # finds out what the whole thing covers and in what order.
            outline=(_series_roadmap(
                context.plan, context.knowledge_map, options.lang)
                if index == 1 and total > 1 else ""),
            closing=total > 1 and index == total))

    # 模型可能用 [[IMG:N]] 引用了参考资料的配图：把被引用的图片下载到本地
    # （data/media/），再把占位符展开为 ![](/media/...) 路径。
    refs = referenced_images(result.body_md, saved_articles)
    local_map: dict[str, str] = {}
    if refs and context.config is not None:
        _emit(progress, "write",
              f"第 {index}/{total} 章《{title}》：正在本地化 "
              f"{len(refs)} 张参考配图…", current=index, total=total,
              stats=stats)
        local_map = localize_reference_images(
            refs, context.config,
            progress=lambda m: _emit(
                progress, "write", f"参考配图 {m}",
                current=index, total=total, stats=stats))
    # Runs even when nothing was downloaded: whatever cannot be resolved has
    # to be cleared out rather than shipped as a literal [[IMG:N]].
    result.body_md = expand_image_refs(
        result.body_md, saved_articles, local_map)

    primary = saved_articles[0]
    body_md = _series_nav_note(
        options.topic, index, context.titles(),
        chapter["prerequisites"], options.lang) + result.body_md
    draft = Draft(
        article_id=primary.id or 0,
        title_candidates=result.title_candidates,
        body_md=body_md,
        topic=options.topic,
        source_url=primary.url,
        source_name=primary.source_name,
        flagged_claims=result.flagged_claims,
        origin="series",
        sources=[
            {
                "article_id": article.id,
                "title": article.title,
                "url": article.url,
                "source_name": article.source_name,
                "published_at": (article.published_at.isoformat()
                                 if article.published_at else None),
                "rank": rank,
                "role": "primary" if rank == 1 else "source",
            }
            for rank, article in enumerate(saved_articles, 1)
        ],
        citations=result.citations,
        search_meta={
            "topic": options.topic,
            "chapter": title,
            "queries": chapter["search_queries"],
            "lang": options.lang,
            "depth": options.depth,
            "ref_count": options.ref_count,
            "style_id": options.style_id,
            "engines": list(options.engines),
        },
        series_id=context.series_id,
        series_order=index,
        series_part_label=_part_label(index, options.lang),
        prerequisites=chapter["prerequisites"],
    )
    sanitize_draft(draft, context.sensitive_words)
    saved_draft = store.save_draft(draft)
    summary = _chapter_summary(result.body_md)
    store.update_chapter(chapter_id, status="done", draft_id=saved_draft.id,
                         summary=summary, error=None)
    _emit(progress, "write",
          f"第 {index}/{total} 章《{title}》已完成，草稿 #{saved_draft.id}",
          current=index, total=total, stats=stats)
    return saved_draft.id, summary


def plan_series(store, provider: LLMProvider, options: SeriesCreateOptions, *,
                status: str = "running",
                progress: ProgressCallback | None = None,
                should_stop: Callable[[], bool] | None = None,
                stats: dict | None = None
                ) -> tuple[int, list[dict], dict[int, list[SearchHit]]]:
    """Decide what the series will cover, and record it.

    Everything up to the first chapter being written: grounding, the field's
    shape, the outline, and the feasibility check. Cheap enough — searches and
    two model calls — to be worth doing before the user commits to the run.
    """
    options.validate()
    grounding = probe_topic(
        options, progress=progress, should_stop=should_stop, stats=stats)

    _check_cancel(should_stop)
    _emit(progress, "map", "正在梳理该领域的核心脉络…", stats=stats)
    knowledge_map = generate_knowledge_map(
        options.topic, grounding, provider, depth=options.depth,
        lang=options.lang)

    _check_cancel(should_stop)
    _emit(progress, "outline", "正在生成系列提纲…", stats=stats)
    chapters = generate_series_outline(
        options.topic, grounding, provider, parts=options.parts,
        depth=options.depth, lang=options.lang, knowledge_map=knowledge_map)

    series_id = store.create_series(
        title=options.topic, topic=options.topic, lang=options.lang,
        depth=options.depth, parts=len(chapters), style_id=options.style_id,
        knowledge_map=knowledge_map, ref_count=options.ref_count,
        engines=list(options.engines), status=status)
    total = len(chapters)
    if stats is not None:
        stats["series_id"] = series_id
        stats["parts"] = total
    _emit(progress, "outline", f"提纲已生成，共 {total} 章",
          current=0, total=total, stats=stats)

    seeds: dict[int, list[SearchHit]] = {}
    try:
        chapters, seeds = preflight_chapters(
            chapters, options, provider, grounding=grounding,
            progress=progress, should_stop=should_stop, stats=stats)
    except SearchCreateCancelled:
        store.finish_series(series_id, "cancelled")
        raise
    except Exception as exc:  # noqa: BLE001
        # Losing the check costs a chance to fix the outline, not the run.
        logger.warning("章节预检失败：%s", exc)

    store.add_chapters(series_id, chapters)
    return series_id, chapters, seeds


def run_series_chapters(store, provider: LLMProvider, series_id: int, *,
                        chapter_ids: list[int] | None = None,
                        seeds: dict[int, list[SearchHit]] | None = None,
                        style=None, promotion_footer: str = "",
                        seo_tags_enabled: bool = True,
                        sensitive_words: set[str] | None = None,
                        workers: int = 6, proxy: str | None = None,
                        config: Config | None = None,
                        stats: dict | None = None,
                        progress: ProgressCallback | None = None,
                        should_stop: Callable[[], bool] | None = None) -> dict:
    """Write the chapters of a planned series, in order.

    Reads what to write from the chapter rows rather than from an outline in
    memory, so a reviewed outline, a resumed run and a single-chapter rerun all
    take the same path.
    """
    series = store.get_series(series_id)
    if series is None:
        raise ValueError("系列不存在")
    rows = store.list_chapters(series_id)
    targets = [row for row in rows
               if (chapter_ids is None or row["id"] in chapter_ids)
               and not row["draft_id"]]
    options = _series_options(series, workers=workers, proxy=proxy)
    search_options = options.chapter_options()
    search_options.validate()
    stats = stats if stats is not None else {}
    stats.setdefault("series_id", series_id)
    stats.setdefault("topic", options.topic)
    stats.setdefault("parts", len(rows))
    stats.setdefault("chapters_done", 0)
    stats.setdefault("chapters_failed", 0)

    if targets:
        store.mark_series_running(series_id)
    total = len(rows)
    # Sources already spoken for, and the chapters the reader has already read.
    seen_urls = {_url_key(url) for url in store.series_source_urls(series_id)}
    written = [{"order": row["chapter_order"], "title": row["title"],
                "summary": row["summary"] or ""}
               for row in rows if row["status"] == "done"]
    draft_ids: list[int] = []
    context = _ChapterContext(
        store=store, provider=provider, options=options,
        search_options=search_options, series_id=series_id, style=style,
        promotion_footer=promotion_footer, seo_tags_enabled=seo_tags_enabled,
        sensitive_words=sensitive_words or set(),
        plan=[{"order": row["chapter_order"], "title": row["title"],
               "scope": row["scope"] or ""} for row in rows],
        knowledge_map=(series["knowledge_map"]
                       if "knowledge_map" in series.keys() else "") or "",
        config=config)

    for row in targets:
        index = row["chapter_order"]
        title = row["title"]
        try:
            draft_id, summary = _write_chapter(
                context, _chapter_from_row(row), row["id"], index=index,
                total=total, seen_urls=seen_urls, written=written,
                hits=(seeds or {}).get(index), progress=progress,
                should_stop=should_stop, stats=stats)
            draft_ids.append(draft_id)
            written.append(
                {"order": index, "title": title, "summary": summary})
            written.sort(key=lambda item: item["order"])
            stats["chapters_done"] += 1
        except SearchCreateCancelled:
            store.update_chapter(row["id"], status="cancelled")
            store.finish_series(series_id, "cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            # One thin chapter must not discard the chapters already written,
            # so every failure is recorded on the chapter and the run moves on.
            logger.warning("系列第 %d 章失败：%s", index, exc)
            stats["chapters_failed"] += 1
            store.update_chapter(row["id"], status="failed", error=str(exc))
            _emit(progress, "research",
                  f"第 {index}/{total} 章《{title}》未完成：{exc}",
                  current=index, total=total, stats=stats)

    status = store.refresh_series_status(series_id)
    if len(targets) == 1 and chapter_ids is not None:
        row = targets[0]
        _emit(progress, "done",
              f"第 {row['chapter_order']} 章《{row['title']}》"
              + ("已重新生成" if draft_ids else "仍未完成"),
              current=row["chapter_order"], total=total, stats=stats)
    else:
        _emit(progress, "done",
              f"系列完成：{stats['chapters_done']} 章成功，"
              f"{stats['chapters_failed']} 章失败",
              current=total, total=total, stats=stats)
    return {
        "series_id": series_id,
        "status": status,
        "draft_ids": draft_ids,
        "stats": stats,
    }


def run_series_create(store, provider: LLMProvider,
                      options: SeriesCreateOptions, *, style=None,
                      promotion_footer: str = "",
                      seo_tags_enabled: bool = True,
                      sensitive_words: set[str] | None = None,
                      config: Config | None = None,
                      progress: ProgressCallback | None = None,
                      should_stop: Callable[[], bool] | None = None) -> dict:
    """Plan and write a series in one go, without stopping for review."""
    options.validate()
    stats = {
        "topic": options.topic,
        "lang": options.lang,
        "depth": options.depth,
        "parts": options.parts,
        "ref_count": options.ref_count,
        "style_id": options.style_id,
        "engines": list(options.engines),
        "series_id": None,
        "grounding_hits": 0,
        "chapters_done": 0,
        "chapters_failed": 0,
    }
    series_id, _, seeds = plan_series(
        store, provider, options, progress=progress, should_stop=should_stop,
        stats=stats)
    return run_series_chapters(
        store, provider, series_id, seeds=seeds, style=style,
        promotion_footer=promotion_footer, seo_tags_enabled=seo_tags_enabled,
        sensitive_words=sensitive_words, workers=options.workers,
        proxy=options.proxy, config=config, stats=stats, progress=progress,
        should_stop=should_stop)


def _loads(value, fallback):
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def _series_options(series, *, workers: int = 6,
                    proxy: str | None = None) -> SeriesCreateOptions:
    """The options the series was created with, so later work matches it."""
    options = SeriesCreateOptions(
        topic=series["topic"], lang=series["lang"], depth=series["depth"],
        # Chapter count no longer decides anything here — the chapter rows do —
        # so a stored count outside the range must not block writing them.
        parts=min(max(int(series["parts"] or MIN_PARTS), MIN_PARTS), MAX_PARTS),
        style_id=series["style_id"],
        engines=_loads(series["engines"], []) or list(DEFAULT_SEARCH_ENGINES),
        ref_count=series["ref_count"] or 5, workers=workers, proxy=proxy)
    options.validate()
    return options


def _chapter_from_row(row) -> dict:
    return {
        "title": row["title"],
        "scope": row["scope"] or "",
        "search_queries": _loads(row["search_queries"], []) or [row["title"]],
        "prerequisites": _loads(row["prerequisites"], []),
    }


def retry_chapter(store, provider: LLMProvider, series_id: int,
                  chapter_id: int, **kwargs) -> dict:
    """Research and write one chapter again, in place.

    The counterpart to chapter-level failure isolation: a chapter that came up
    short is worth another attempt on its own, and rerunning the whole series
    to get it would discard everything that did work.
    """
    if store.get_series(series_id) is None:
        raise ValueError("系列不存在")
    row = next((item for item in store.list_chapters(series_id)
                if item["id"] == chapter_id), None)
    if row is None:
        raise ValueError("章节不存在")
    if row["draft_id"]:
        raise ValueError("该章已有草稿，请在草稿编辑页修改，避免覆盖已有改动")
    result = run_series_chapters(
        store, provider, series_id, chapter_ids=[chapter_id],
        stats={"chapter_id": chapter_id, "chapter": row["title"]}, **kwargs)
    result["chapter_id"] = chapter_id
    return result
