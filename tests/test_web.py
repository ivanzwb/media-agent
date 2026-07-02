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
        article_id=art.id, title_candidates=["原标题"],
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


def test_publish_wechat_requires_credentials(tmp_path):
    # No WeChat AppID/AppSecret configured -> 400 with a helpful message.
    client, store, _ = make_client(tmp_path)
    _, draft = seed(store)
    r = client.post(f"/drafts/{draft.id}/publish/wechat",
                    data={"mode": "draft", "kind": "article"})
    assert r.status_code == 400
    assert r.json()["ok"] is False
    assert "AppID" in r.json()["error"]


def test_publish_wechat_article_happy_path(tmp_path, monkeypatch):
    client, store, _ = make_client(tmp_path)
    _, draft = seed(store)
    store.set_setting("wechat_appid", "wx123")
    store.set_setting("wechat_appsecret", "secret")

    class FakeClient:
        def __init__(self, *a, **k):
            pass
    # get_wechat_client (re-exported from app.wechat) returns a truthy client;
    # publish_article is mocked so no real network happens.
    monkeypatch.setattr("app.wechat.get_wechat_client",
                        lambda cfg: FakeClient())
    monkeypatch.setattr("app.wechat.publish.publish_article",
                        lambda cli, cfg, meta, mode="draft": {
                            "ok": True, "draft_media_id": "DRAFT1",
                            "title": "原标题", "mode": mode})
    r = client.post(f"/drafts/{draft.id}/publish/wechat",
                    data={"mode": "draft", "kind": "article"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["draft_media_id"] == "DRAFT1"


def test_channels_prepare_returns_caption(tmp_path):
    # 视频号 half-auto prepare: returns a caption + the create URL. No video
    # was generated, so has_video is False (still ok=True).
    client, store, _ = make_client(tmp_path)
    _, draft = seed(store)
    r = client.post(f"/drafts/{draft.id}/wechat-channels/prepare")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["title"]
    assert j["create_url"].startswith("https://channels.weixin.qq.com")
    assert j["has_video"] is False


def test_video_prepare_toutiao(tmp_path):
    client, store, _ = make_client(tmp_path)
    _, draft = seed(store)
    r = client.post(f"/drafts/{draft.id}/video-prepare",
                    data={"platform": "toutiao"})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["create_url"].startswith("https://mp.toutiao.com")
    assert j["has_video"] is False
    assert j["title"]


def test_video_prepare_unsupported_platform(tmp_path):
    # zhihu has no video_publish_url -> half-auto video not supported.
    client, store, _ = make_client(tmp_path)
    _, draft = seed(store)
    r = client.post(f"/drafts/{draft.id}/video-prepare",
                    data={"platform": "zhihu"})
    assert r.status_code == 400
    assert r.json()["ok"] is False


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
    import time
    client, store, _ = make_client(tmp_path)
    r = client.post("/run")
    assert r.status_code == 200
    assert r.json()["started"] is True

    # Run executes in a background thread; wait for it to finish.
    deadline = time.time() + 10
    while time.time() < deadline:
        status = client.get("/api/run-status").json()
        if not status["running"] and status["finished_at"]:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("run did not finish in time")

    assert status["error"] is None
    assert len(store.list_runs()) == 1


def test_archive_shows_rewrite_state(tmp_path):
    client, store, _ = make_client(tmp_path)
    art, _draft = seed(store)
    r = client.get("/archive")
    assert r.status_code == 200
    assert "已转写" in r.text  # seeded article already has a draft


def test_draft_edit_has_tabs(tmp_path):
    client, store, _ = make_client(tmp_path)
    _art, draft = seed(store)
    r = client.get(f"/drafts/{draft.id}/edit")
    assert r.status_code == 200
    assert 'id="tab-article"' in r.text
    assert 'id="tab-video"' in r.text
    assert "文章内容" in r.text and "讲解视频" in r.text


def test_archive_view_renders_original(tmp_path):
    client, store, _ = make_client(tmp_path)
    art, _draft = seed(store)
    r = client.get(f"/archive/{art.id}/view")
    assert r.status_code == 200
    assert "GPT-5 breakthrough" in r.text       # title
    assert "返回归档" in r.text                   # view chrome
    assert "article-body" in r.text


def test_archive_refetch(tmp_path, monkeypatch):
    client, store, _ = make_client(tmp_path)
    art, _draft = seed(store)
    import app.sources.scraper as scr
    monkeypatch.setattr(scr, "scrape_single", lambda url, name, **k: Article(
        title="t2", content_md="# New body\nrefetched", url=url,
        source_name=name, source_type="scrape", published_at=None,
        images=["https://i/a.png"], raw_summary=None,
        fetched_at=datetime.now(timezone.utc), videos=["https://v/b.mp4"]))
    r = client.post(f"/archive/{art.id}/refetch")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["images"] == 1 and d["videos"] == 1
    body = store.read_article_body(art.id)
    assert "New body" in body["content_md"]
    assert body["videos"] == ["https://v/b.mp4"]


def test_archive_rewrite_article(tmp_path):
    client, store, _ = make_client(tmp_path)
    art = store.save_article(Article(
        title="No draft yet", content_md="# Body\nsome facts here",
        url="https://x.com/b", source_name="OpenAI", source_type="rss",
        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc), images=[],
        raw_summary=None, fetched_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        topic="AI"))
    assert store.get_draft_for_article(art.id) is None

    r = client.post(f"/archive/{art.id}/rewrite")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["draft_id"]
    # now there is a draft for this article
    assert store.get_draft_for_article(art.id) is not None
    # rewrite again should overwrite (still exactly one draft)
    r2 = client.post(f"/archive/{art.id}/rewrite")
    assert r2.json()["ok"] is True
    drafts_for_article = [d for d in store.list_drafts()
               if d["article_id"] == art.id]
    assert len(drafts_for_article) == 1


def test_export_config(tmp_path):
    client, store, _ = make_client(tmp_path)
    r = client.get("/export")
    assert r.status_code == 200
    data = r.json()
    assert "feeds" in data and "settings" in data
    assert any(t["name"] == "AI" for t in data["feeds"]["topics"])


def test_import_config_applies_feeds_and_skips_secrets(tmp_path):
    import json as _json
    client, store, feeds = make_client(tmp_path)
    payload = {
        "version": 1,
        "feeds": {
            "topics": [{"name": "Tech", "keywords": ["x"]}],
            "sources": [{"name": "S", "type": "rss", "url": "http://s/feed",
                         "topics": ["Tech"]}],
        },
        "settings": {"llm_provider": "openai", "llm_api_key": "sk-secret"},
    }
    files = {"file": ("c.json", _json.dumps(payload).encode(), "application/json")}
    r = client.post("/import", files=files, follow_redirects=False)
    assert r.status_code == 303

    from app.feeds import load_feeds
    cfg = load_feeds(feeds)
    assert any(t.name == "Tech" for t in cfg.topics)
    assert any(s.url == "http://s/feed" for s in cfg.sources)
    # non-secret setting imported, API key skipped
    assert store.get_setting("llm_provider") == "openai"
    assert not store.get_setting("llm_api_key")


def test_settings_ok(tmp_path):
    client, store, _ = make_client(tmp_path)
    r = client.get("/settings")
    assert r.status_code == 200
    assert "LLM Provider" in r.text


def test_settings_save_schedule(tmp_path):
    client, store, _ = make_client(tmp_path)
    r = client.post("/settings", data={
        "schedule_cron": "0 8 * * *", "schedule_enabled": "1"},
        follow_redirects=False)
    assert r.status_code == 303
    assert store.get_setting("schedule_cron") == "0 8 * * *"
    assert store.get_setting("schedule_enabled") == "1"


def test_sources_discover_from_url(tmp_path, monkeypatch):
    client, store, feeds = make_client(tmp_path)
    from app.feeds import SourceConfig
    monkeypatch.setattr(
        "app.web.server.discover_from_url",
        lambda url, topics=None: [SourceConfig(
            name="Found Blog", type="rss", url="https://found.com/feed.xml",
            topics=topics or [])])
    r = client.post("/sources/discover",
                    data={"url": "https://found.com", "topics": "AI"},
                    follow_redirects=False)
    assert r.status_code == 303
    from app.feeds import load_feeds
    cfg = load_feeds(feeds)
    assert any(s.url == "https://found.com/feed.xml" for s in cfg.sources)


def test_sources_search_adds(tmp_path, monkeypatch):
    client, store, feeds = make_client(tmp_path)
    from app.feeds import SourceConfig
    monkeypatch.setattr(
        "app.web.server.discover_from_keyword",
        lambda keyword, topics=None: [SourceConfig(
            name="KW Site", type="scrape", url="https://kw.com/",
            topics=topics or [], mode="list")])
    r = client.post("/sources/search",
                    data={"keyword": "physical ai", "topics": "物理AI"},
                    follow_redirects=False)
    assert r.status_code == 303
    from app.feeds import load_feeds
    cfg = load_feeds(feeds)
    assert any(s.url == "https://kw.com/" for s in cfg.sources)


def test_draft_adapt_returns_json(tmp_path):
    client, store, _ = make_client(tmp_path)
    art, draft = seed(store)
    r = client.post(f"/drafts/{draft.id}/adapt",
                    data={"platform": "xiaohongshu"})
    assert r.status_code == 200
    data = r.json()
    assert "body_md" in data
    assert "title_candidates" in data
    assert "publish_url" in data
    # Should NOT create a new draft
    assert len(store.list_drafts()) == 1
