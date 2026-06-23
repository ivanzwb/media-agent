from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.config import Config
from app.db import connect, init_db
from app.models import Article, Draft
from app.store import Store
from app.web.server import create_app


def setup_env(tmp_path):
    cfg = Config(data_dir=tmp_path, llm_provider="mock", image_provider="mock")
    cfg.ensure_dirs()
    conn = connect(cfg.db_path)
    init_db(conn)
    store = Store(conn, cfg)
    return cfg, store


def seed(store):
    art = store.save_article(Article(
        title="GPT-5 breakthrough", content_md="# Body\nfacts",
        url="https://x.com/a", source_name="OpenAI", source_type="rss",
        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc), images=[],
        raw_summary=None, fetched_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        topic="AI"))
    draft = store.save_draft(Draft(
        article_id=art.id, platform="master", title_candidates=["原标题"],
        body_md="## 钩子\n正文内容", topic="AI", source_url=art.url,
        source_name=art.source_name, flagged_claims=["可疑说法X"]))
    return art, draft


def make_client(tmp_path):
    cfg, store = setup_env(tmp_path)
    feeds = tmp_path / "feeds.yaml"
    feeds.write_text(
        "topics:\n  - name: AI\n    keywords: [GPT]\nsources: []\n",
        encoding="utf-8")
    app = create_app(cfg, feeds_path=feeds)
    return TestClient(app), store, feeds


def test_dashboard_ok(tmp_path):
    client, store, _ = make_client(tmp_path)
    r = client.get("/")
    assert r.status_code == 200
    assert "Media Agent" in r.text


def test_api_stats(tmp_path):
    client, store, _ = make_client(tmp_path)
    seed(store)
    r = client.get("/api/stats")
    assert r.status_code == 200
    data = r.json()
    assert data["articles"] == 1 and data["drafts"] == 1


def test_archive_lists_and_filters(tmp_path):
    client, store, _ = make_client(tmp_path)
    seed(store)
    r = client.get("/archive")
    assert r.status_code == 200
    assert "GPT-5 breakthrough" in r.text
    r2 = client.get("/archive?topic=AI")
    assert "GPT-5 breakthrough" in r2.text


def test_draft_edit_and_save(tmp_path):
    client, store, _ = make_client(tmp_path)
    art, draft = seed(store)
    r = client.get(f"/drafts/{draft.id}/edit")
    assert r.status_code == 200
    assert "正文内容" in r.text
    assert "可疑说法X" in r.text  # flagged claims shown

    r2 = client.post(f"/drafts/{draft.id}", data={
        "title_candidates": "新标题A\n新标题B",
        "body_md": "## 改写后的正文\n更好了",
        "status": "approved"}, follow_redirects=False)
    assert r2.status_code == 303
    reloaded = store.read_draft_body(draft.id)
    assert "改写后的正文" in reloaded["body_md"]
    assert reloaded["title_candidates"] == ["新标题A", "新标题B"]
    assert store.get_draft(draft.id)["status"] == "approved"


def test_sources_view_and_add(tmp_path):
    client, store, feeds = make_client(tmp_path)
    r = client.get("/sources")
    assert r.status_code == 200
    r2 = client.post("/sources/add", data={
        "name": "DeepMind", "type": "scrape",
        "url": "https://deepmind.google/blog/", "topics": "AI,物理AI",
        "mode": "list", "include_pattern": "/blog/"},
        follow_redirects=False)
    assert r2.status_code == 303
    from app.feeds import load_feeds
    cfg = load_feeds(feeds)
    assert any(s.name == "DeepMind" for s in cfg.sources)


def test_trigger_run(tmp_path):
    client, store, _ = make_client(tmp_path)
    r = client.post("/run", follow_redirects=False)
    assert r.status_code == 303
    assert len(store.list_runs()) == 1


def test_settings_ok(tmp_path):
    client, store, _ = make_client(tmp_path)
    r = client.get("/settings")
    assert r.status_code == 200
    assert "LLM Provider" in r.text
