from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.feeds import FeedsConfig
from app.images.base import ImageProvider
from app.llm.base import LLMProvider
from app.models import Article
from app.sources.dedup import dedup
from app.sources.rss import fetch_feed
from app.sources.scraper import scrape_list, scrape_single
from app.store import Store
from app.pipeline.classifier import classify
from app.pipeline.images import attach_cover
from app.pipeline.rewriter import rewrite


def collect_sources(feeds: FeedsConfig,
                    max_per_source: int | None = None) -> list[Article]:
    articles: list[Article] = []
    for src in feeds.sources:
        if not src.enabled:
            continue
        try:
            if src.type == "rss":
                items = fetch_feed(src.url, src.name)
            elif src.type == "scrape":
                if src.mode == "list":
                    items = scrape_list(
                        src.url, src.name, include_pattern=src.include_pattern)
                else:
                    art = scrape_single(src.url, src.name)
                    items = [art] if art else []
            else:
                items = []
        except Exception:
            continue
        if max_per_source:
            items = items[:max_per_source]
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


def run_pipeline(feeds: FeedsConfig, store: Store, provider: LLMProvider,
                 max_drafts: int = 10,
                 image_provider: ImageProvider | None = None,
                 record: bool = False,
                 max_age_days: int | None = None,
                 max_per_source: int | None = None) -> dict:
    stats = {"fetched": 0, "archived": 0, "classified": 0, "drafted": 0}
    run_id = store.record_run() if record else None

    try:
        articles = collect_sources(feeds, max_per_source=max_per_source)
        articles = filter_by_age(articles, max_age_days)
        articles = dedup(articles)
        stats["fetched"] = len(articles)

        new_articles: list[Article] = []
        for art in articles:
            if store.exists(art.fingerprint()):
                continue
            art.topic = classify(art, feeds.topics, provider)
            saved = store.save_article(art)
            stats["archived"] += 1
            stats["classified"] += 1
            new_articles.append(saved)

        for art in new_articles[:max_drafts]:
            draft = rewrite(art, provider)
            saved_draft = store.save_draft(draft)
            stats["drafted"] += 1
            if image_provider is not None:
                attach_cover(saved_draft, image_provider, store.config.images_dir)
                store.set_draft_cover(saved_draft.id, saved_draft.cover_image)
    finally:
        if run_id is not None:
            store.finish_run(run_id, json.dumps(stats, ensure_ascii=False))

    return stats
