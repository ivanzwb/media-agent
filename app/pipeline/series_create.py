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
from app.pipeline.rewriter import _extract_json
from app.pipeline.sanitizer import sanitize_draft
from app.pipeline.search_create import (
    ProgressCallback, SearchCreateCancelled, SearchCreateOptions, _check_cancel,
    _emit, _topic_search_terms, _url_key, collect_references, search_queries)
from app.pipeline.synthesizer import synthesize
from app.sources.web_search import DEFAULT_SEARCH_ENGINES, SearchHit

logger = logging.getLogger(__name__)

MIN_PARTS = 3
MAX_PARTS = 10
DEPTHS = ("beginner", "intermediate", "advanced")

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
    parts: int = 5
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


_LIST_LINE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")


def _clean_markdown_list(raw: str) -> str:
    """Keep the nested list and drop everything the model wrapped it in.

    The map is rendered as Markdown in the UI, so a stray prose preamble or
    code fence would show up verbatim.
    """
    lines: list[str] = []
    for line in str(raw or "").splitlines():
        if line.strip().startswith("```"):
            continue
        if _LIST_LINE.match(line):
            lines.append(line.rstrip())
        elif lines and not line.strip():
            break  # the list has ended; whatever follows is commentary
    text = "\n".join(lines)
    return text[:_MAP_CHARS].rstrip()


def generate_knowledge_map(topic: str, grounding: str, provider: LLMProvider,
                           *, depth: str, lang: str = "zh") -> str:
    """Sketch the shape of the field as a nested Markdown list.

    Written before the outline so the chapter split follows the subject's own
    structure rather than a generic "background, principles, practice" arc.
    Rendered as Markdown rather than a diagram: the frontend already renders
    Markdown, and a nested list carries the hierarchy without a new dependency.
    """
    language = {"zh": "中文", "en": "English",
                "bilingual": "中英双语"}.get(lang, "中文")
    grounding_block = (
        f"该主题公开资料的标题与摘要，用于校准术语：\n{grounding}\n\n"
        if grounding.strip() else "")
    prompt = (
        f"梳理知识主题「{topic}」的核心脉络。深度定位：{_DEPTH_LABELS[depth]}。"
        f"输出语言：{language}。\n\n"
        f"{grounding_block}"
        "只输出一个嵌套的 Markdown 无序列表，最多三层：第一层是该领域的主要分支，"
        "第二层是分支下的关键概念或方法，第三层可选，用于补充典型应用或常见误区。"
        "每项一行，控制在 20 字以内。\n"
        "不要输出标题、说明文字或代码围栏。"
    )
    try:
        return _clean_markdown_list(
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
                            *, parts: int, depth: str, lang: str = "zh",
                            knowledge_map: str = "") -> list[dict]:
    language = {"zh": "中文", "en": "English",
                "bilingual": "中英双语"}.get(lang, "中文")
    grounding_block = (
        "下面是该主题公开资料的标题与摘要，只用于校准术语用词和覆盖面，"
        f"不要当作事实来源，也不要照抄：\n{grounding}\n\n"
        if grounding.strip() else ""
    )
    map_block = (
        f"该领域的核心脉络如下，章节划分应覆盖其中的主要分支：\n{knowledge_map}\n\n"
        if knowledge_map.strip() else ""
    )
    prompt = (
        f"为知识主题「{topic}」设计一个 {parts} 篇的系列文章提纲。\n"
        f"深度定位：{_DEPTH_LABELS[depth]}。输出语言：{language}。\n"
        "章节之间要有清晰的递进关系，共同覆盖该领域的主要脉络，"
        "彼此不重复。\n\n"
        f"{map_block}{grounding_block}"
        "只输出 JSON 对象：\n"
        '{"chapters":[{"title":"章节标题",'
        '"scope":"这一章讲什么、读者读完能得到什么，100 字以内",'
        '"search_queries":["2-4 个用于检索该章资料的检索词，短而具体，'
        '使用该领域实际通用的说法"],'
        '"prerequisites":["需要先读的本系列其他章节标题"]}]}\n'
        f"chapters 必须恰好 {parts} 项。不要输出代码围栏或额外说明。"
    )
    raw = provider.chat([Message(role="user", content=prompt)])
    chapters = _normalise_chapters(
        (_extract_json(raw) or {}).get("chapters"), topic, parts, lang)
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
        context_note=_chapter_context(written, options.lang))

    primary = saved_articles[0]
    draft = Draft(
        article_id=primary.id or 0,
        title_candidates=result.title_candidates,
        body_md=result.body_md,
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


def run_series_create(store, provider: LLMProvider,
                      options: SeriesCreateOptions, *, style=None,
                      promotion_footer: str = "",
                      seo_tags_enabled: bool = True,
                      sensitive_words: set[str] | None = None,
                      progress: ProgressCallback | None = None,
                      should_stop: Callable[[], bool] | None = None) -> dict:
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
        engines=list(options.engines))
    stats["series_id"] = series_id
    stats["parts"] = len(chapters)
    total = len(chapters)
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

    chapter_ids = store.add_chapters(series_id, chapters)
    search_options = options.chapter_options()
    search_options.validate()
    seen_urls: set[str] = set()
    written: list[dict] = []
    draft_ids: list[int] = []

    context = _ChapterContext(
        store=store, provider=provider, options=options,
        search_options=search_options, series_id=series_id, style=style,
        promotion_footer=promotion_footer, seo_tags_enabled=seo_tags_enabled,
        sensitive_words=sensitive_words or set())

    for index, (chapter, chapter_id) in enumerate(
            zip(chapters, chapter_ids), 1):
        title = chapter["title"]
        try:
            draft_id, summary = _write_chapter(
                context, chapter, chapter_id, index=index, total=total,
                seen_urls=seen_urls, written=written, hits=seeds.get(index),
                progress=progress, should_stop=should_stop, stats=stats)
            draft_ids.append(draft_id)
            written.append(
                {"order": index, "title": title, "summary": summary})
            stats["chapters_done"] += 1
        except SearchCreateCancelled:
            store.update_chapter(chapter_id, status="cancelled")
            store.finish_series(series_id, "cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            # One thin chapter must not discard the chapters already written,
            # so every failure is recorded on the chapter and the run moves on.
            logger.warning("系列第 %d 章失败：%s", index, exc)
            stats["chapters_failed"] += 1
            store.update_chapter(chapter_id, status="failed", error=str(exc))
            _emit(progress, "research",
                  f"第 {index}/{total} 章《{title}》未完成：{exc}",
                  current=index, total=total, stats=stats)

    if stats["chapters_done"] == 0:
        status = "failed"
    elif stats["chapters_failed"]:
        status = "partial"
    else:
        status = "done"
    store.finish_series(series_id, status)
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


def _loads(value, fallback):
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def retry_chapter(store, provider: LLMProvider, series_id: int,
                  chapter_id: int, *, style=None, promotion_footer: str = "",
                  seo_tags_enabled: bool = True,
                  sensitive_words: set[str] | None = None,
                  workers: int = 6, proxy: str | None = None,
                  progress: ProgressCallback | None = None,
                  should_stop: Callable[[], bool] | None = None) -> dict:
    """Research and write one chapter again, in place.

    The counterpart to chapter-level failure isolation: a chapter that came up
    short is worth another attempt on its own, and rerunning the whole series
    to get it would discard everything that did work. Reuses the series' own
    search settings so the rerun is comparable to the original attempt.
    """
    series = store.get_series(series_id)
    if series is None:
        raise ValueError("系列不存在")
    chapters = store.list_chapters(series_id)
    row = next((item for item in chapters if item["id"] == chapter_id), None)
    if row is None:
        raise ValueError("章节不存在")
    if row["draft_id"]:
        raise ValueError("该章已有草稿，请在草稿编辑页修改，避免覆盖已有改动")

    options = SeriesCreateOptions(
        topic=series["topic"], lang=series["lang"], depth=series["depth"],
        parts=series["parts"], style_id=series["style_id"],
        engines=_loads(series["engines"], []) or list(DEFAULT_SEARCH_ENGINES),
        ref_count=series["ref_count"] or 5, workers=workers, proxy=proxy)
    options.validate()
    search_options = options.chapter_options()
    search_options.validate()

    chapter = {
        "title": row["title"],
        "scope": row["scope"] or "",
        "search_queries": _loads(row["search_queries"], []) or [row["title"]],
        "prerequisites": _loads(row["prerequisites"], []),
    }
    order = row["chapter_order"]
    stats = {
        "topic": options.topic,
        "series_id": series_id,
        "chapter_id": chapter_id,
        "chapter": row["title"],
        "parts": len(chapters),
        "chapters_done": 0,
        "chapters_failed": 0,
    }
    # The other chapters' sources stay off-limits, exactly as during the run.
    seen_urls = {_url_key(url)
                 for url in store.series_source_urls(series_id)}
    written = [{"order": item["chapter_order"], "title": item["title"],
                "summary": item["summary"] or ""}
               for item in chapters
               if item["chapter_order"] < order and item["status"] == "done"]

    context = _ChapterContext(
        store=store, provider=provider, options=options,
        search_options=search_options, series_id=series_id, style=style,
        promotion_footer=promotion_footer, seo_tags_enabled=seo_tags_enabled,
        sensitive_words=sensitive_words or set())
    try:
        draft_id, _ = _write_chapter(
            context, chapter, chapter_id, index=order, total=len(chapters),
            seen_urls=seen_urls, written=written, progress=progress,
            should_stop=should_stop, stats=stats)
    except SearchCreateCancelled:
        store.update_chapter(chapter_id, status="cancelled")
        store.refresh_series_status(series_id)
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("系列第 %d 章重跑失败：%s", order, exc)
        stats["chapters_failed"] = 1
        store.update_chapter(chapter_id, status="failed", error=str(exc))
        status = store.refresh_series_status(series_id)
        _emit(progress, "done", f"第 {order} 章《{row['title']}》仍未完成：{exc}",
              current=order, total=len(chapters), stats=stats)
        return {"series_id": series_id, "chapter_id": chapter_id,
                "status": status, "draft_ids": [], "stats": stats}

    stats["chapters_done"] = 1
    status = store.refresh_series_status(series_id)
    _emit(progress, "done", f"第 {order} 章《{row['title']}》已重新生成",
          current=order, total=len(chapters), stats=stats)
    return {"series_id": series_id, "chapter_id": chapter_id,
            "status": status, "draft_ids": [draft_id], "stats": stats}
