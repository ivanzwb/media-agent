import json
from datetime import datetime, timezone

from app.config import Config
from app.db import connect, init_db
from app.store import Store
from app.models import Article
from app.feeds import FeedsConfig, Topic, SourceConfig
from app.llm.providers.mock import MockProvider
from app.images.providers.mock import MockImageProvider
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


def test_run_pipeline_attaches_cover_and_records(tmp_path, monkeypatch):
    cfg, store = build(tmp_path)
    feeds = FeedsConfig(
        topics=[Topic(name="AI", keywords=["GPT"])],
        sources=[SourceConfig(name="X", type="rss", url="https://x.com/feed",
                              topics=["AI"])])
    monkeypatch.setattr("app.pipeline.orchestrator.collect_sources",
                        lambda feeds_cfg, max_per_source=None: fake_articles())
    provider = MockProvider(responses=[
        json.dumps({"title_candidates": ["爆款标题"], "body_md": "## 钩子\n正文"}),
        json.dumps({"flagged_claims": []})])

    stats = run_pipeline(feeds, store, provider, max_drafts=5,
                         image_provider=MockImageProvider(), record=True)
    assert stats["drafted"] == 1

    draft = store.list_drafts()[0]
    assert draft["cover_image"]
    assert (cfg.images_dir / draft["cover_image"]).exists()

    runs = store.list_runs()
    assert len(runs) == 1 and runs[0]["status"] == "done"
