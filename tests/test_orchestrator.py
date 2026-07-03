import json
from datetime import datetime, timezone

from app.config import Config
from app.db import connect, init_db
from app.store import Store
from app.models import Article
from app.feeds import FeedsConfig, Topic, SourceConfig
from app.llm.providers.mock import MockProvider
from app.pipeline.orchestrator import run_pipeline


def build(tmp_path):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    conn = connect(cfg.db_path)
    init_db(conn)
    return cfg, Store(conn, cfg)


def fake_articles():
    return [Article(title="GPT-5 released", content_md="It scores high.",
                    url="https://x.com/a", source_name="X", source_type="rss",
                    published_at=None, images=[], raw_summary=None,
                    fetched_at=datetime.now(timezone.utc))]


def test_run_pipeline_end_to_end(tmp_path, monkeypatch):
    cfg, store = build(tmp_path)
    feeds = FeedsConfig(
        topics=[Topic(name="AI", keywords=["GPT"])],
        sources=[SourceConfig(name="X", type="rss", url="https://x.com/feed",
                              topics=["AI"])])

    monkeypatch.setattr("app.pipeline.orchestrator.collect_sources",
                        lambda feeds_cfg, max_per_source=None, progress=None, workers=None: fake_articles())

    rewrite_json = json.dumps({"title_candidates": ["爆款标题"],
                               "body_md": "## 钩子\n正文"})
    check_json = json.dumps({"flagged_claims": []})
    provider = MockProvider(responses=[rewrite_json, check_json])

    stats = run_pipeline(feeds, store, provider, max_drafts=5)
    assert stats["fetched"] == 1
    assert stats["archived"] == 1
    assert stats["drafted"] == 1

    drafts = store.conn.execute("SELECT * FROM drafts").fetchall()
    assert len(drafts) == 1
    articles = store.conn.execute("SELECT * FROM articles").fetchall()
    assert articles[0]["topic"] == "AI"


def test_run_pipeline_skips_duplicates(tmp_path, monkeypatch):
    cfg, store = build(tmp_path)
    feeds = FeedsConfig(topics=[Topic(name="AI", keywords=["GPT"])], sources=[])
    monkeypatch.setattr("app.pipeline.orchestrator.collect_sources",
                        lambda feeds_cfg, max_per_source=None, progress=None, workers=None: fake_articles())
    provider = MockProvider(responses=[
        json.dumps({"title_candidates": ["t"], "body_md": "b"}),
        json.dumps({"flagged_claims": []})])
    run_pipeline(feeds, store, provider, max_drafts=5)
    stats2 = run_pipeline(feeds, store, provider, max_drafts=5)
    assert stats2["archived"] == 0


def test_run_pipeline_skips_same_title_source(tmp_path, monkeypatch):
    """Articles with same title+source but different URLs (different fingerprints)
    are still deduped by the secondary exists_by_title_source() check."""
    cfg, store = build(tmp_path)

    # First run: saves article with URL-A
    monkeypatch.setattr("app.pipeline.orchestrator.collect_sources",
                        lambda feeds_cfg, max_per_source=None, progress=None, workers=None: [
        Article(title="Same Title", content_md="body", url="https://x.com/url-a",
                source_name="Test Source", source_type="rss", published_at=None,
                images=[], raw_summary=None,
                fetched_at=datetime.now(timezone.utc))])
    feeds = FeedsConfig(topics=[Topic(name="AI", keywords=["title"])], sources=[])
    provider = MockProvider(responses=[
        json.dumps({"title_candidates": ["t1"], "body_md": "b1"}),
        json.dumps({"flagged_claims": []})])
    stats1 = run_pipeline(feeds, store, provider, max_drafts=5)
    assert stats1["archived"] == 1

    # Second run: same title+source, different URL → should be skipped
    monkeypatch.setattr("app.pipeline.orchestrator.collect_sources",
                        lambda feeds_cfg, max_per_source=None, progress=None, workers=None: [
        Article(title="Same Title", content_md="body", url="https://x.com/url-b",
                source_name="Test Source", source_type="rss", published_at=None,
                images=[], raw_summary=None,
                fetched_at=datetime.now(timezone.utc))])
    provider2 = MockProvider(responses=[
        json.dumps({"title_candidates": ["t2"], "body_md": "b2"}),
        json.dumps({"flagged_claims": []})])
    stats2 = run_pipeline(feeds, store, provider2, max_drafts=5)
    assert stats2["archived"] == 0
    # Only 1 article in DB
    count = store.conn.execute("SELECT COUNT(*) AS c FROM articles").fetchone()["c"]
    assert count == 1
