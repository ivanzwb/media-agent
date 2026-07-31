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

from dataclasses import dataclass
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
from app.sources.web_search import DEFAULT_SEARCH_ENGINES

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
                            *, parts: int, depth: str,
                            lang: str = "zh") -> list[dict]:
    language = {"zh": "中文", "en": "English",
                "bilingual": "中英双语"}.get(lang, "中文")
    grounding_block = (
        "下面是该主题公开资料的标题与摘要，只用于校准术语用词和覆盖面，"
        f"不要当作事实来源，也不要照抄：\n{grounding}\n\n"
        if grounding.strip() else ""
    )
    prompt = (
        f"为知识主题「{topic}」设计一个 {parts} 篇的系列文章提纲。\n"
        f"深度定位：{_DEPTH_LABELS[depth]}。输出语言：{language}。\n"
        "章节之间要有清晰的递进关系，共同覆盖该领域的主要脉络，"
        "彼此不重复。\n\n"
        f"{grounding_block}"
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
                        progress: ProgressCallback | None,
                        should_stop: Callable[[], bool] | None,
                        stats: dict | None) -> list[Article]:
    """References for one chapter, widening the query once if it comes up short.

    A narrow chapter — a glossary, a niche variant — is normal in a series, and
    the outline's terms for it are the most likely to miss. Retrying with the
    series topic prepended recovers most of those before the chapter is written
    off.
    """
    articles = collect_references(
        chapter["search_queries"], search_options, provider,
        topic=chapter["title"], seen_urls=seen_urls, progress=progress,
        should_stop=should_stop, stats=stats)
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
    _emit(progress, "outline", "正在生成系列提纲…", stats=stats)
    chapters = generate_series_outline(
        options.topic, grounding, provider, parts=options.parts,
        depth=options.depth, lang=options.lang)

    series_id = store.create_series(
        title=options.topic, topic=options.topic, lang=options.lang,
        depth=options.depth, parts=len(chapters), style_id=options.style_id)
    chapter_ids = store.add_chapters(series_id, chapters)
    stats["series_id"] = series_id
    stats["parts"] = len(chapters)
    total = len(chapters)
    _emit(progress, "outline", f"提纲已生成，共 {total} 章",
          current=0, total=total, stats=stats)

    search_options = options.chapter_options()
    search_options.validate()
    seen_urls: set[str] = set()
    written: list[dict] = []
    draft_ids: list[int] = []

    for index, (chapter, chapter_id) in enumerate(
            zip(chapters, chapter_ids), 1):
        title = chapter["title"]
        try:
            _check_cancel(should_stop)
            store.update_chapter(chapter_id, status="researching", error=None)
            _emit(progress, "research",
                  f"第 {index}/{total} 章《{title}》：正在检索资料…",
                  current=index, total=total, stats=stats)
            articles = _chapter_references(
                chapter, search_options, provider, topic=options.topic,
                seen_urls=seen_urls, progress=progress,
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
            _emit(progress, "write",
                  f"第 {index}/{total} 章《{title}》：正在写作…",
                  current=index, total=total, stats=stats)
            result = synthesize(
                f"{options.topic}：{title}", saved_articles, provider,
                style=style, lang=options.lang,
                promotion_footer=promotion_footer,
                seo_tags_enabled=seo_tags_enabled,
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
                        "published_at": (
                            article.published_at.isoformat()
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
                series_id=series_id,
                series_order=index,
                series_part_label=_part_label(index, options.lang),
                prerequisites=chapter["prerequisites"],
            )
            sanitize_draft(draft, sensitive_words or set())
            saved_draft = store.save_draft(draft)
            summary = _chapter_summary(result.body_md)
            store.update_chapter(
                chapter_id, status="done", draft_id=saved_draft.id,
                summary=summary, error=None)
            draft_ids.append(saved_draft.id)
            written.append(
                {"order": index, "title": title, "summary": summary})
            stats["chapters_done"] += 1
            _emit(progress, "write",
                  f"第 {index}/{total} 章《{title}》已完成，草稿 #{saved_draft.id}",
                  current=index, total=total, stats=stats)
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
