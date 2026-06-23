import json
from datetime import datetime, timedelta, timezone

from app.config import Config
from app.db import connect, init_db
from app.store import Store
from app.models import Article
from app.feeds import FeedsConfig, Topic, SourceConfig
from app.llm.providers.mock import MockProvider
from app.pipeline.orchestrator import (collect_sources, filter_by_age,
                                       run_pipeline)


def art(title, days_old=None, url=None):
    pub = None
    if days_old is not None:
        pub = datetime.now(timezone.utc) - timedelta(days=days_old)
    return Article(title=title, content_md="GPT body", url=url or f"https://x.com/{title}",
                   source_name="X", source_type="rss", published_at=pub,
                   images=[], raw_summary=None,
                   fetched_at=datetime.now(timezone.utc))


def test_filter_by_age_drops_old_keeps_recent_and_none():
    items = [art("recent", days_old=1), art("old", days_old=40),
             art("undated", days_old=None)]
    out = filter_by_age(items, max_age_days=7)
    titles = {a.title for a in out}
    assert titles == {"recent", "undated"}


def test_filter_by_age_none_returns_all():
    items = [art("a", days_old=100)]
    assert len(filter_by_age(items, None)) == 1


def test_collect_sources_caps_per_source(monkeypatch):
    feeds = FeedsConfig(topics=[], sources=[
        SourceConfig(name="X", type="rss", url="https://x.com/feed")])
    many = [art(f"a{i}") for i in range(5)]
    monkeypatch.setattr("app.pipeline.orchestrator.fetch_feed",
                        lambda url, name: many)
    out = collect_sources(feeds, max_per_source=2)
    assert len(out) == 2


def test_run_pipeline_applies_controls(tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    conn = connect(cfg.db_path)
    init_db(conn)
    store = Store(conn, cfg)
    feeds = FeedsConfig(topics=[Topic(name="AI", keywords=["GPT"])],
                        sources=[SourceConfig(name="X", type="rss",
                                              url="https://x.com/feed")])
    items = [art("recent1", days_old=1), art("recent2", days_old=2),
             art("old", days_old=90)]
    monkeypatch.setattr("app.pipeline.orchestrator.fetch_feed",
                        lambda url, name: items)
    provider = MockProvider(responses=[
        json.dumps({"title_candidates": ["t"], "body_md": "b"}),
        json.dumps({"flagged_claims": []})] * 5)

    stats = run_pipeline(feeds, store, provider, max_drafts=10,
                         max_age_days=7, max_per_source=10)
    assert stats["fetched"] == 2  # old dropped by age filter
