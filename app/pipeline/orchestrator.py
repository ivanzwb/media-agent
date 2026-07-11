from __future__ import annotations

import json
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from app.feeds import FeedsConfig
from app.images.base import ImageProvider
from app.llm.base import LLMProvider
from app.models import Article, Draft
from app.sources.dedup import dedup
from app.sources.failure_log import log_fetch_failure
from app.sources.rss import fetch_feed
from app.sources.scraper import scrape_list, scrape_single
from app.store import Store
from app.pipeline.classifier import classify
from app.pipeline.images import attach_cover, inject_image_prompt
from app.pipeline.localize import localize_article
from app.pipeline.rewriter import rewrite
from app.pipeline.sanitizer import load_words, sanitize_draft
from app.pipeline.score import compute_draft_score


def _fetch_source(src, proxy: str | None = None) -> list[Article]:
    if src.type == "rss":
        return fetch_feed(src.url, src.name, proxy=proxy)
    if src.type == "scrape":
        if src.mode == "list":
            return scrape_list(
                src.url, src.name, include_pattern=src.include_pattern,
                exclude_pattern=src.exclude_pattern,
                max_pages=getattr(src, "max_pages", 3) or 3,
                render_js=getattr(src, "render_js", True), proxy=proxy)
        art = scrape_single(src.url, src.name,
                            render_js=getattr(src, "render_js", True),
                            proxy=proxy)
        return [art] if art else []
    return []


def collect_sources(feeds: FeedsConfig,
                    max_per_source: int | None = None,
                    progress=None, workers: int | None = None,
                    proxy: str | None = None) -> list[Article]:
    enabled = [s for s in feeds.sources if s.enabled]
    if not enabled:
        return []
    workers = max(1, workers or (os.cpu_count() or 4))
    total = len(enabled)
    _counter = [0]  # mutable thread-safe counter for closure

    def fetch_one(src):
        _counter[0] += 1
        idx = _counter[0]
        if progress:
            progress(f"抓取来源 [{idx}/{total}]：{src.name}")
        # Stagger concurrent requests to avoid tripping rate limits (429).
        time.sleep(random.uniform(0.3, 1.5))
        try:
            items = _fetch_source(src, proxy=proxy)
        except Exception as e:  # noqa: BLE001
            # Log detailed failure info
            fetch_url = getattr(src, "url", "")
            log_fetch_failure(
                source_name=src.name,
                source_type=src.type,
                url=fetch_url,
                exception=e,
            )
            if progress:
                err_cls = type(e).__name__
                # Include status code in the progress message when available
                status = getattr(e, "response", None)
                status_code = getattr(status, "status_code", None)
                if status_code:
                    progress(f"  来源失败：{src.name}（{err_cls} {status_code}）")
                else:
                    progress(f"  来源失败：{src.name}（{err_cls}）")
            return []
        if max_per_source:
            items = items[:max_per_source]
        if progress:
            progress(f"  {src.name} 获取 {len(items)} 篇")
        return items

    articles: list[Article] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        # ex.map preserves input order so results are deterministic.
        for items in ex.map(fetch_one, enabled):
            articles.extend(items)
    return articles


def filter_by_age(articles: list[Article], max_age_days: int | None,
                  now: datetime | None = None) -> list[Article]:
    if not max_age_days:
        return articles
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    return [a for a in articles
            if a.published_at is None or a.published_at >= cutoff]


def _apply_promotion_footer(draft: Draft, config) -> None:
    """Append configured promotion footer to draft body_md."""
    footer = config.promotion_footer
    if footer:
        draft.body_md = draft.body_md.rstrip() + f"\n\n---\n{footer}\n"


def _append_prompt(draft: Draft, store: Store) -> None:
    """Append an image-generation prompt placeholder to the draft body."""
    meta = store.read_draft_body(draft.id)
    body = meta.get("body_md", "")
    titles = meta.get("title_candidates") or draft.title_candidates or []
    prompt_draft = Draft(
        article_id=draft.article_id,
        title_candidates=titles,
        body_md="",  # not used, we build from existing body
        topic=meta.get("topic", ""),
        source_url=meta.get("source_url", ""),
        source_name=meta.get("source_name", ""),
        status=meta.get("status", "drafted"),
    )
    prompt_block = inject_image_prompt(prompt_draft)
    store.update_draft_body(draft.id, titles, body + prompt_block)


def _noop(*_args, **_kwargs) -> None:
    pass


def run_pipeline(feeds: FeedsConfig, store: Store, provider: LLMProvider,
                 max_drafts: int = 10,
                 image_provider: ImageProvider | None = None,
                 record: bool = False,
                 max_age_days: int | None = None,
                 max_per_source: int | None = None,
                 download_images: bool = True,
                 download_videos: bool = True,
                 progress=None,
                 rewrite_gate=None,
                 style=None) -> dict:
    emit = progress or _noop
    stats = {"fetched": 0, "archived": 0, "classified": 0, "drafted": 0}
    run_id = store.record_run() if record else None
    sens_words = load_words(store.config)

    try:
        emit("开始抓取来源…", stats)
        articles = collect_sources(feeds, max_per_source=max_per_source,
                                   progress=lambda m: emit(m, stats),
                                   workers=store.config.workers,
                                   proxy=store.config.fetch_proxy)
        emit(f"抓取完成，共 {len(articles)} 篇，去重中…", stats)
        articles = filter_by_age(articles, max_age_days)
        articles = dedup(articles)
        stats["fetched"] = len(articles)
        emit(f"去重后 {len(articles)} 篇，开始归档分类…", stats)

        new_articles: list[Article] = []
        for art in articles:
            if store.exists(art.fingerprint()):
                continue
            if store.exists_by_title_source(art.title, art.source_name):
                continue
            art.topic = classify(art, feeds.topics, provider)
            if download_images or download_videos:
                emit(f"下载媒体到本地：{art.title[:40]}", stats)
                try:
                    localize_article(art, store.config,
                                     download_images=download_images,
                                     download_videos=download_videos,
                                     progress=lambda m: emit(m, stats))
                except Exception as e:  # noqa: BLE001
                    emit(f"  媒体本地化失败（保留远程链接）：{e}", stats)
            saved = store.save_article(art)
            stats["archived"] += 1
            stats["classified"] += 1
            new_articles.append(saved)
            emit(f"归档[{art.topic}]：{art.title[:50]}", stats)

        to_draft = new_articles[:max_drafts]
        emit(f"新归档 {len(new_articles)} 篇，开始改写 {len(to_draft)} 篇…", stats)
        for art in to_draft:
            # Free-tier rewrite quota: gate() returns False once exhausted.
            if rewrite_gate is not None and not rewrite_gate():
                emit("改写额度已用完（免费版每日限额），跳过后续改写。"
                     "升级 Pro 版可无限改写。", stats)
                break
            emit(f"改写中：{art.title[:50]}", stats)
            try:
                draft = rewrite(art, provider, style=style,
                                promotion_footer=store.config.promotion_footer)
                if sens_words:
                    hits = sanitize_draft(draft, sens_words)
                    if hits:
                        n = sum(h["count"] for h in hits)
                        emit(f"  敏感词过滤 {n} 处：{art.title[:40]}", stats)
                saved_draft = store.save_draft(draft)
                stats["drafted"] += 1
                # Compute and store draft score for quick filtering
                try:
                    score_val = compute_draft_score(
                        title=draft.title_candidates[0] if draft.title_candidates else "",
                        topic=draft.topic,
                        source_name=draft.source_name,
                        body_preview=draft.body_md,
                        published_at=art.published_at,
                        provider=provider,
                    )
                    store.update_draft_score(saved_draft.id, score_val)
                    emit(f"  评分：{score_val}/100", stats)
                except Exception as exc:
                    emit(f"  评分失败（已跳过）：{exc}", stats)
                if image_provider is not None:
                    attach_cover(saved_draft, image_provider,
                                 store.config.images_dir)
                    if saved_draft.cover_image:
                        store.set_draft_cover(saved_draft.id,
                                              saved_draft.cover_image)
                    else:
                        _append_prompt(saved_draft, store)
                else:
                    _append_prompt(saved_draft, store)
                emit(f"  已生成草稿：{art.title[:50]}", stats)
            except Exception as e:
                emit(f"  改写失败（已跳过）：{art.title[:40]}（{e}）", stats)
        emit("流水线完成", stats)
    finally:
        if run_id is not None:
            store.finish_run(run_id, json.dumps(stats, ensure_ascii=False))

    return stats
