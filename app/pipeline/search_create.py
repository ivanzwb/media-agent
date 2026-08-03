from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import logging
import re
from typing import Callable
from urllib.parse import urlsplit

from app.config import Config
from app.llm.base import LLMProvider, Message
from app.models import Article, Draft
from app.pipeline.orchestrator import filter_by_age
from app.pipeline.relevance import filter_relevant
from app.pipeline.rewriter import _extract_json
from app.pipeline.sanitizer import sanitize_draft
from app.pipeline.localize import localize_reference_images
from app.pipeline.synthesizer import (
    expand_image_refs, referenced_images, synthesize)
from app.sources.dedup import dedup, dedup_near_content
from app.sources.scraper import scrape_single
from app.sources.web_search import (
    AVAILABLE_SEARCH_ENGINES, DEFAULT_SEARCH_ENGINES, SearchHit,
    merge_search_hits, search_query)

ProgressCallback = Callable[[dict], None]
logger = logging.getLogger(__name__)


class SearchCreateCancelled(RuntimeError):
    pass


@dataclass
class SearchCreateOptions:
    topic: str
    lang: str = "zh"
    time_range_days: int | None = 30
    ref_count: int = 10
    style_id: str | None = None
    engines: tuple[str, ...] | list[str] = DEFAULT_SEARCH_ENGINES
    max_results_per_query: int = 8
    workers: int = 6
    search_timeout: int = 8
    scrape_timeout: float = 25.0
    proxy: str | None = None

    def validate(self) -> None:
        self.topic = self.topic.strip()
        if not self.topic:
            raise ValueError("请输入搜索创作主题")
        if len(self.topic) > 200:
            raise ValueError("搜索创作主题不能超过 200 个字符")
        if self.lang not in ("zh", "en", "bilingual"):
            raise ValueError("搜索语言必须为 zh、en 或 bilingual")
        if self.time_range_days not in (None, 0, 7, 30, 90):
            raise ValueError("时间范围必须为 7、30、90 天或不限")
        if self.ref_count not in (5, 10, 20):
            raise ValueError("参考文章数量必须为 5、10 或 20")
        selected = list(dict.fromkeys(
            str(engine).strip().lower() for engine in self.engines
            if str(engine).strip()))
        invalid = [engine for engine in selected
                   if engine not in AVAILABLE_SEARCH_ENGINES]
        if invalid:
            raise ValueError(f"不支持的搜索引擎：{', '.join(invalid)}")
        if not selected:
            raise ValueError("请至少选择一个搜索引擎")
        self.engines = tuple(selected)
        self.workers = max(1, min(int(self.workers or 1), 12))


def _check_cancel(should_stop: Callable[[], bool] | None) -> None:
    if should_stop and should_stop():
        raise SearchCreateCancelled("搜索创作已取消")


def _emit(progress: ProgressCallback | None, stage: str, detail: str, *,
          current: int = 0, total: int = 0, stats: dict | None = None) -> None:
    if progress:
        progress({
            "stage": stage,
            "detail": detail,
            "current": current,
            "total": total,
            "stats": dict(stats or {}),
        })


_ZH_QUESTION_TAIL = re.compile(
    r"(?:好吗|好不好|行不行|对不对|可以吗|能不能|是不是|会不会|有没有"
    r"|怎么样|怎么办|为什么|是否|如何)$")
_ZH_MODAL_WORDS = re.compile(r"真的|到底|究竟|其实|确实|应该")


def _topic_search_terms(topic: str, lang: str) -> str:
    """Reduce a conversational topic to search terms.

    Search engines match natural-language questions poorly, so punctuation
    becomes term separators and language-level question/modal words are
    dropped. Only function words are removed; subject matter is preserved.
    """
    compact = " ".join(re.sub(r"[,，。、！？!?；;：:]+", " ", topic).split())
    if lang == "en":
        return compact
    segments = []
    for segment in compact.split():
        segment = _ZH_MODAL_WORDS.sub("", segment)
        segment = _ZH_QUESTION_TAIL.sub("", segment)
        if segment:
            segments.append(segment)
    return " ".join(segments) or compact or topic


def expand_search_queries(topic: str, lang: str,
                          provider: LLMProvider) -> list[str]:
    language = {
        "zh": "以中文检索词为主",
        "en": "只使用英文检索词",
        "bilingual": "同时给出中文和英文检索词",
    }[lang]
    prompt = (
        f"为主题「{topic}」生成 3-5 个适合查找高质量新闻、研究和深度文章的"
        f"搜索引擎检索词。{language}。检索词要短而具体，把最有区分度的核心"
        "概念放在最前面，不要照抄完整问句；同时覆盖专业解释、风险/收益、"
        "专家建议和可靠数据。"
        '只输出 JSON：{"queries":["..."]}。'
    )
    try:
        raw = provider.chat([Message(role="user", content=prompt)])
        parsed = _extract_json(raw) or {}
        queries = parsed.get("queries") or []
        cleaned = [str(query).strip() for query in queries if str(query).strip()]
        if cleaned:
            base = _topic_search_terms(topic, lang)
            return list(dict.fromkeys([base, *cleaned]))[:5]
        logger.warning(
            "检索词扩展未返回有效 queries，使用本地兜底；响应：%r",
            str(raw)[:300])
    except Exception as exc:  # noqa: BLE001
        logger.warning("检索词扩展失败，使用本地兜底：%s", exc)

    base = _topic_search_terms(topic, lang)
    if lang == "en":
        return [base, f"{base} research", f"{base} expert analysis"]
    if lang == "bilingual":
        return [base, f"{base} 分析", f"{base} research review"]
    return [base, f"{base} 分析", f"{base} 专家 建议"]


def _search_timelimit(days: int | None) -> str | None:
    return {7: "w", 30: "m", 90: "y"}.get(days or 0)


def _search_region(lang: str) -> str:
    return {"zh": "cn-zh", "en": "us-en"}.get(lang, "wt-wt")


def search_queries(queries: list[str], options: SearchCreateOptions, *,
                   progress: ProgressCallback | None = None,
                   should_stop: Callable[[], bool] | None = None,
                   stats: dict | None = None) -> list[SearchHit]:
    groups: list[list[SearchHit]] = [[] for _ in queries]
    with ThreadPoolExecutor(max_workers=min(4, len(queries) or 1)) as executor:
        futures = {
            executor.submit(
                search_query,
                query,
                max_results=options.max_results_per_query,
                timelimit=_search_timelimit(options.time_range_days),
                region=_search_region(options.lang),
                engines=options.engines,
                proxy=options.proxy,
                timeout=options.search_timeout,
            ): index
            for index, query in enumerate(queries)
        }
        done = 0
        for future in as_completed(futures):
            _check_cancel(should_stop)
            index = futures[future]
            try:
                groups[index] = future.result()
            except Exception:  # noqa: BLE001
                groups[index] = []
            done += 1
            _emit(progress, "search", f"已完成检索：{queries[index]}",
                  current=done, total=len(queries), stats=stats)
    return merge_search_hits(groups)


def scrape_search_hits(hits: list[SearchHit], options: SearchCreateOptions, *,
                       progress: ProgressCallback | None = None,
                       should_stop: Callable[[], bool] | None = None,
                       stats: dict | None = None) -> list[Article]:
    articles: list[Article] = []

    def scrape(hit: SearchHit) -> Article | None:
        host = urlsplit(hit.url).netloc.removeprefix("www.") or hit.title
        return scrape_single(
            hit.url,
            host,
            timeout=options.scrape_timeout,
            render_js=False,
            proxy=options.proxy,
        )

    executor = ThreadPoolExecutor(max_workers=options.workers)
    futures = {executor.submit(scrape, hit): hit for hit in hits}
    done = 0
    try:
        for future in as_completed(futures):
            _check_cancel(should_stop)
            hit = futures[future]
            try:
                article = future.result()
                if article is not None:
                    articles.append(article)
            except Exception:  # noqa: BLE001
                pass
            done += 1
            if stats is not None:
                stats["scraped"] = len(articles)
            _emit(progress, "scrape", f"正在抓取：{urlsplit(hit.url).netloc}",
                  current=done, total=len(hits), stats=stats)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return articles


def _topic_terms(topic: str) -> set[str]:
    lowered = topic.lower()
    words = set(re.findall(r"[a-z0-9]{2,}", lowered))
    cjk = "".join(re.findall(r"[\u3400-\u9fff]", lowered))
    words.update(cjk[index:index + 2] for index in range(max(0, len(cjk) - 1)))
    if cjk:
        words.update(cjk)
    return words


def _lexical_topic_score(article: Article, terms: set[str]) -> float:
    title = (article.title or "").lower()
    body = (article.content_md or article.raw_summary or "").lower()[:4000]
    relevance = sum(title.count(term) * 8 + body.count(term)
                    for term in terms)
    richness = min(len(body) / 1000, 4)
    return relevance + richness


def _select_source_diverse(ranked: list[Article], limit: int) -> list[Article]:
    selected: list[Article] = []
    deferred: list[Article] = []
    source_counts: dict[str, int] = {}
    for article in ranked:
        source = (article.source_name or urlsplit(article.url).netloc).lower()
        if source_counts.get(source, 0) >= 2:
            deferred.append(article)
            continue
        selected.append(article)
        source_counts[source] = source_counts.get(source, 0) + 1
        if len(selected) >= limit:
            return selected
    selected.extend(deferred[:max(0, limit - len(selected))])
    return selected[:limit]


def rank_by_topic(articles: list[Article], topic: str, provider: LLMProvider,
                  limit: int) -> list[Article]:
    if not articles:
        return []
    listing = "\n\n".join(
        f"[{index}] {article.title}\n{(article.content_md or '')[:500]}"
        for index, article in enumerate(articles)
    )
    prompt = (
        f"按与主题「{topic}」的相关性、信息密度和可信度为候选文章评分。"
        '只输出 JSON：{"scores":[{"index":0,"score":95}]}，必须包含每个编号。'
        f"\n\n{listing}"
    )
    try:
        parsed = _extract_json(
            provider.chat([Message(role="user", content=prompt)])) or {}
        raw_scores = parsed.get("scores") or []
        scores: dict[int, float] = {}
        for item in raw_scores:
            if not isinstance(item, dict):
                continue
            index = int(item.get("index"))
            if 0 <= index < len(articles):
                scores[index] = float(item.get("score", 0))
    except Exception:  # noqa: BLE001
        scores = {}
    terms = _topic_terms(topic)
    ranked = sorted(
        enumerate(articles),
        key=lambda pair: (
            scores.get(pair[0], 0),
            _lexical_topic_score(pair[1], terms),
        ),
        reverse=True,
    )
    return _select_source_diverse(
        [article for _, article in ranked], limit)


def _url_key(url: str) -> str:
    parts = urlsplit(str(url or "").strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    return f"{host}{path}?{parts.query}" if parts.query else f"{host}{path}"


def collect_references(queries: list[str], options: SearchCreateOptions,
                       provider: LLMProvider, *, topic: str | None = None,
                       seen_urls: set[str] | None = None,
                       hits: list[SearchHit] | None = None,
                       progress: ProgressCallback | None = None,
                       should_stop: Callable[[], bool] | None = None,
                       stats: dict | None = None) -> list[Article]:
    """Search, scrape, dedup, filter and rank references for one piece.

    Shared by single-draft search creation and per-chapter series research so
    the two never drift apart. Returns however many references survive —
    deciding whether that is enough is the caller's business, since a series
    tolerates a starved chapter while a single draft does not.

    ``seen_urls`` scopes deduplication above one piece: pass a set of
    ``_url_key`` values to keep later chapters off the sources earlier ones
    already used. It is only read here — callers decide which of the returned
    references they actually keep, and record those themselves.

    ``hits`` skips the search when the caller already has results for these
    queries, which is how a series reuses what its feasibility check found
    instead of paying for the same search twice.
    """
    topic = topic or options.topic
    _check_cancel(should_stop)
    if hits is None:
        _emit(progress, "search", "正在搜索相关文章…",
              total=len(queries), stats=stats)
        hits = search_queries(
            queries, options, progress=progress, should_stop=should_stop,
            stats=stats)
    if seen_urls is not None:
        hits = [hit for hit in hits if _url_key(hit.url) not in seen_urls]
    if stats is not None:
        stats["urls"] = len(hits)
    if not hits:
        return []

    _check_cancel(should_stop)
    _emit(progress, "scrape", f"准备抓取 {len(hits)} 个页面…",
          total=len(hits), stats=stats)
    articles = scrape_search_hits(
        hits, options, progress=progress, should_stop=should_stop, stats=stats)
    articles = dedup(articles)
    before_near_dedup = len(articles)
    articles = dedup_near_content(articles)
    near_duplicates = before_near_dedup - len(articles)
    if stats is not None:
        stats["near_duplicates"] = near_duplicates
    if near_duplicates:
        _emit(
            progress, "filter",
            f"内容近重：移除 {near_duplicates} 篇转载或近似页面", stats=stats)

    _check_cancel(should_stop)
    _emit(progress, "filter", "正在过滤低质量和不相关内容…", stats=stats)
    age_filtered = filter_by_age(
        articles, options.time_range_days,
        progress=lambda message: _emit(
            progress, "filter", message, stats=stats))
    if options.time_range_days and len(age_filtered) < 2:
        if stats is not None:
            stats["time_range_relaxed"] = True
        _emit(
            progress, "filter",
            f"最近 {options.time_range_days} 天资料不足，已自动扩大到不限时间",
            stats=stats)
    else:
        articles = age_filtered
    articles = filter_relevant(
        articles, provider, enabled=True, content_scope="informational",
        progress=lambda message: _emit(
            progress, "filter", message, stats=stats))
    return rank_by_topic(articles, topic, provider, options.ref_count)


def run_search_create(store, provider: LLMProvider,
                      options: SearchCreateOptions, *, style=None,
                      promotion_footer: str = "",
                      seo_tags_enabled: bool = True,
                      sensitive_words: set[str] | None = None,
                      config: Config | None = None,
                      progress: ProgressCallback | None = None,
                      should_stop: Callable[[], bool] | None = None) -> dict:
    options.validate()
    stats = {
        "topic": options.topic,
        "lang": options.lang,
        "time_range_days": options.time_range_days or 0,
        "ref_count": options.ref_count,
        "style_id": options.style_id,
        "engines": list(options.engines),
        "queries": 0,
        "urls": 0,
        "scraped": 0,
        "near_duplicates": 0,
        "time_range_relaxed": False,
        "kept": 0,
        "draft_id": None,
    }

    _check_cancel(should_stop)
    _emit(progress, "expand", "正在扩展检索词…", stats=stats)
    queries = expand_search_queries(options.topic, options.lang, provider)
    stats["queries"] = len(queries)

    articles = collect_references(
        queries, options, provider, progress=progress,
        should_stop=should_stop, stats=stats)
    if not stats["urls"]:
        raise ValueError("没有搜索到可用的文章链接")
    if len(articles) < 2:
        raise ValueError("有效参考资料不足 2 篇，请调整主题或时间范围后重试")
    stats["kept"] = len(articles)

    saved_articles: list[Article] = []
    for article in articles:
        _check_cancel(should_stop)
        article.topic = options.topic
        saved_articles.append(store.save_article(article))

    _check_cancel(should_stop)
    _emit(progress, "synthesize", f"正在综合 {len(saved_articles)} 篇资料…",
          stats=stats)
    result = synthesize(
        options.topic, saved_articles, provider,
        style=style, lang=options.lang,
        promotion_footer=promotion_footer,
        seo_tags_enabled=seo_tags_enabled)

    # 模型可能用 [[IMG:N]] 引用了参考资料的配图：把被引用的图片下载到本地
    # （data/media/），再把占位符展开为 ![](/media/...) 路径。
    refs = referenced_images(result.body_md, saved_articles)
    local_map: dict[str, str] = {}
    if refs and config is not None:
        _emit(progress, "synthesize",
              f"正在本地化正文引用的 {len(refs)} 张参考配图…", stats=stats)
        local_map = localize_reference_images(
            refs, config,
            progress=lambda m: _emit(
                progress, "synthesize", f"参考配图 {m}", stats=stats))
    # Runs even when nothing was downloaded: whatever cannot be resolved has
    # to be cleared out rather than shipped as a literal [[IMG:N]].
    result.body_md = expand_image_refs(
        result.body_md, saved_articles, local_map)

    _check_cancel(should_stop)
    _emit(progress, "fact_check", "多源事实校验完成，正在保存草稿…",
          stats=stats)
    primary = saved_articles[0]
    source_refs = [
        {
            "article_id": article.id,
            "title": article.title,
            "url": article.url,
            "source_name": article.source_name,
            "published_at": (
                article.published_at.isoformat() if article.published_at else None
            ),
            "rank": index,
            "role": "primary" if index == 1 else "source",
        }
        for index, article in enumerate(saved_articles, 1)
    ]
    draft = Draft(
        article_id=primary.id or 0,
        title_candidates=result.title_candidates,
        body_md=result.body_md,
        topic=options.topic,
        source_url=primary.url,
        source_name=primary.source_name,
        flagged_claims=result.flagged_claims,
        digest=result.digest,
        origin="search_create",
        sources=source_refs,
        citations=result.citations,
        search_meta={
            "topic": options.topic,
            "queries": queries,
            "lang": options.lang,
            "time_range_days": options.time_range_days or 0,
            "ref_count": options.ref_count,
            "style_id": options.style_id,
            "engines": list(options.engines),
        },
    )
    sanitize_draft(draft, sensitive_words or set())
    saved_draft = store.save_draft(draft)
    stats["draft_id"] = saved_draft.id
    _emit(progress, "done", f"已保存草稿 #{saved_draft.id}", stats=stats)
    return {
        "draft_id": saved_draft.id,
        "article_ids": [article.id for article in saved_articles],
        "queries": queries,
        "sources": source_refs,
        "stats": stats,
    }
