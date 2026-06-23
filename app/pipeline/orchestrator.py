from __future__ import annotations

from app.feeds import FeedsConfig
from app.llm.base import LLMProvider
from app.models import Article
from app.sources.dedup import dedup
from app.sources.rss import fetch_feed
from app.sources.scraper import scrape_list, scrape_single
from app.store import Store
from app.pipeline.classifier import classify
from app.pipeline.rewriter import rewrite


def collect_sources(feeds: FeedsConfig) -> list[Article]:
    articles: list[Article] = []
    for src in feeds.sources:
        if not src.enabled:
            continue
        try:
            if src.type == "rss":
                articles.extend(fetch_feed(src.url, src.name))
            elif src.type == "scrape":
                if src.mode == "list":
                    articles.extend(scrape_list(
                        src.url, src.name, include_pattern=src.include_pattern))
                else:
                    art = scrape_single(src.url, src.name)
                    if art:
                        articles.append(art)
        except Exception:
            continue
    return articles


def run_pipeline(feeds: FeedsConfig, store: Store, provider: LLMProvider,
                 max_drafts: int = 10) -> dict:
    stats = {"fetched": 0, "archived": 0, "classified": 0, "drafted": 0}

    articles = dedup(collect_sources(feeds))
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
        store.save_draft(draft)
        stats["drafted"] += 1

    return stats
