from datetime import datetime, timezone
import time

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


def make_client(tmp_path, pro: bool = True):
    cfg, store = setup_env(tmp_path)
    feeds = tmp_path / "feeds.yaml"
    feeds.write_text(
        "topics:\n  - name: AI\n    keywords: [GPT]\nsources: []\n",
        encoding="utf-8")
    app = create_app(cfg, feeds_path=feeds)
    # Default tests to a Pro (dev) license so feature tests aren't gated;
    # pass pro=False to exercise the free-tier gates.
    app.state.license._dev = pro
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
    # Data now comes from the JSON API (page route serves the React shell).
    data = client.get("/api/archive").json()
    titles = [a["title"] for g in data["groups"] for a in g["articles"]]
    assert "GPT-5 breakthrough" in titles
    data2 = client.get("/api/archive?topic=AI").json()
    titles2 = [a["title"] for g in data2["groups"] for a in g["articles"]]
    assert "GPT-5 breakthrough" in titles2


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
                        lambda cli, cfg, meta, mode="draft", **kw: {
                            "ok": True, "draft_media_id": "DRAFT1",
                            "title": "原标题", "mode": mode})
    r = client.post(f"/drafts/{draft.id}/publish/wechat",
                    data={"mode": "draft", "kind": "article"})
    assert r.status_code == 202
    assert r.json()["ok"] is True
    status = {}
    for _ in range(100):
        status = client.get(
            f"/api/draft/{draft.id}/publish/wechat/status").json()
        if status["done"]:
            break
        time.sleep(0.01)
    assert status["ok"] is True
    assert status["result"]["draft_media_id"] == "DRAFT1"


def test_publish_wechat_waits_for_terminal_failure(tmp_path, monkeypatch):
    client, store, _ = make_client(tmp_path)
    _, draft = seed(store)
    store.set_setting("wechat_appid", "wx123")
    store.set_setting("wechat_appsecret", "secret")

    class FakeClient:
        def freepublish_get(self, publish_id):
            return {"publish_status": 4, "errmsg": "content rejected",
                    "fail_idx": [0]}

    monkeypatch.setattr("app.wechat.get_wechat_client",
                        lambda cfg: FakeClient())
    monkeypatch.setattr("app.wechat.publish.publish_article",
                        lambda cli, cfg, meta, mode="draft", **kw: {
                            "ok": True, "draft_media_id": "DRAFT1",
                            "publish_id": "PUB1", "title": "原标题",
                            "mode": mode})
    r = client.post(f"/drafts/{draft.id}/publish/wechat",
                    data={"mode": "publish", "kind": "article"})
    assert r.status_code == 202
    status = {}
    for _ in range(100):
        status = client.get(
            f"/api/draft/{draft.id}/publish/wechat/status").json()
        if status["done"]:
            break
        time.sleep(0.01)
    assert status["ok"] is False
    assert "content rejected" in status["error"]
    assert "PUB1" in status["error"]


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


def test_drafts_batch_delete(tmp_path):
    client, store, _ = make_client(tmp_path)
    _, draft = seed(store)
    assert store.get_draft(draft.id) is not None
    r = client.post("/api/drafts/delete", json={"ids": [draft.id]})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "deleted": 1}
    assert store.get_draft(draft.id) is None


def test_drafts_batch_delete_requires_ids(tmp_path):
    client, store, _ = make_client(tmp_path)
    r = client.post("/api/drafts/delete", json={"ids": []})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_settings_roundtrip_tts_and_avatar(tmp_path):
    client, store, _ = make_client(tmp_path)
    r = client.post("/settings", data={
        "tts_provider": "cosyvoice", "tts_rate": "+10%", "tts_pitch": "+15Hz",
        "tts_instruct": "用亲切的语气",
        "avatar_enabled": "1", "avatar_image": "p.png", "avatar_position": "full",
        # Legacy manual paths are intentionally ignored; the managed runtime
        # owns both source and Python.
        "avatar_provider": "sadtalker", "sadtalker_dir": "/opt/SadTalker",
    }, follow_redirects=False)
    assert r.status_code in (200, 303)
    s = client.get("/api/settings").json()
    assert s["tts_rate"] == "+10%" and s["tts_pitch"] == "+15Hz"
    assert s["tts_instruct"] == "用亲切的语气"
    assert s["avatar_enabled"] is True
    assert s["avatar_image"] == "p.png" and s["avatar_position"] == "full"
    assert s["sadtalker_dir"] == ""
    assert store.get_setting("sadtalker_dir") is None
    # persisted to DB
    assert store.get_setting("tts_pitch") == "+15Hz"
    assert store.get_setting("avatar_enabled") == "1"


def test_drafts_pagination_api(tmp_path):
    client, store, _ = make_client(tmp_path)
    art = store.save_article(Article(
        title="A", content_md="# b", url="https://x.com/p", source_name="S",
        source_type="rss", published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        images=[], raw_summary=None,
        fetched_at=datetime(2026, 1, 2, tzinfo=timezone.utc), topic="AI"))
    for i in range(5):
        store.save_draft(Draft(
            article_id=art.id, title_candidates=[f"T{i}"], body_md=f"# {i}",
            topic="AI", source_url=art.url, source_name=art.source_name))
    page1 = client.get("/api/drafts?limit=2&offset=0").json()
    page2 = client.get("/api/drafts?limit=2&offset=2").json()
    assert page1["total"] == 5 and page2["total"] == 5
    assert len(page1["drafts"]) == 2 and len(page2["drafts"]) == 2
    ids1 = {d["id"] for d in page1["drafts"]}
    ids2 = {d["id"] for d in page2["drafts"]}
    assert ids1.isdisjoint(ids2)


def test_avatar_upload_saves_and_serves(tmp_path):
    client, store, _ = make_client(tmp_path)
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 32
    r = client.post("/api/avatar/upload",
                    files={"file": ("me.png", png, "image/png")})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["avatar_image"].endswith(".png")
    # remembered in settings
    assert store.get_setting("avatar_image") == body["avatar_image"]
    # served back
    got = client.get(f"/avatar/{body['avatar_image']}")
    assert got.status_code == 200 and got.content == png


def test_styled_html_wechat_theme(tmp_path):
    client, store, _ = make_client(tmp_path)
    _, draft = seed(store)
    r = client.post(f"/drafts/{draft.id}/styled-html",
                    data={"platform": "wechat", "theme": "blue"})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["html"].startswith("<section")
    assert "#2f6fb3" in j["html"]          # blue theme accent, inlined
    assert "<style" not in j["html"]       # fully inline-styled


def test_styled_html_toutiao(tmp_path):
    client, store, _ = make_client(tmp_path)
    _, draft = seed(store)
    r = client.post(f"/drafts/{draft.id}/styled-html",
                    data={"platform": "toutiao"})
    assert r.status_code == 200
    assert r.json()["platform"] == "toutiao"


# ── License gating (free tier) ──────────────────────────────────────────────

def test_video_gated_on_free_tier(tmp_path):
    client, store, _ = make_client(tmp_path, pro=False)
    _, draft = seed(store)
    r = client.post(f"/drafts/{draft.id}/video")
    assert r.status_code == 403
    body = r.json()
    assert body["ok"] is False and body.get("upgrade") is True


def test_platform_sync_gated_on_free_tier(tmp_path):
    client, store, _ = make_client(tmp_path, pro=False)
    _, draft = seed(store)
    r = client.post(f"/drafts/{draft.id}/styled-html",
                    data={"platform": "wechat"})
    assert r.status_code == 403
    assert r.json().get("upgrade") is True


def test_auto_discover_gated_on_free_tier(tmp_path):
    client, store, _ = make_client(tmp_path, pro=False)
    r = client.post("/sources/auto-discover", data={"themes": "AI"})
    assert r.status_code == 403
    assert r.json().get("upgrade") is True


def test_topics_suggest_gated_on_free_tier(tmp_path):
    client, store, _ = make_client(tmp_path, pro=False)
    r = client.post("/sources/topics/suggest", data={"themes": "AI"})
    assert r.status_code == 403
    assert r.json().get("upgrade") is True


def test_suggest_sources_gated_on_free_tier(tmp_path):
    client, store, _ = make_client(tmp_path, pro=False)
    r = client.post("/sources/suggest-sources", data={"topic": "AI"})
    assert r.status_code == 403
    assert r.json().get("upgrade") is True


def test_auto_discover_allowed_for_pro(tmp_path):
    """Pro (dev) tier passes the gate (endpoint proceeds past the 403)."""
    client, store, _ = make_client(tmp_path, pro=True)
    r = client.post("/sources/topics/suggest", data={"themes": "AI"})
    assert r.status_code == 200


def test_apply_template_rewrite_quota_exhausted_free_tier(tmp_path):
    """Template rewrite counts against the free daily rewrite quota."""
    from app.licensing import gates as LG, features as LF
    client, store, _ = make_client(tmp_path, pro=False)
    _, draft = seed(store)
    lic = client.app.state.license
    # Exhaust the free daily rewrite quota, then the template rewrite is blocked.
    for _ in range(LF.FREE_REWRITE_PER_DAY):
        LG.consume_rewrite(lic, store)
    r = client.post(f"/api/draft/{draft.id}/apply-template",
                    data={"template_id": "whatever"})
    assert r.status_code == 403
    assert r.json().get("upgrade") is True


def test_license_status_and_activate(tmp_path, monkeypatch):
    import base64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from app.licensing import features as F
    from app.licensing.verify import canonical, encode_activation

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr("app.licensing.verify._public_key_b64",
                        lambda: base64.urlsafe_b64encode(pub).decode().rstrip("="))

    client, store, _ = make_client(tmp_path, pro=False)
    _, draft = seed(store)

    # free tier: styled-html gated
    assert client.post(f"/drafts/{draft.id}/styled-html",
                       data={"platform": "wechat"}).status_code == 403
    assert client.get("/api/license/status").json()["active"] is False

    payload = {"key": "MA-T", "edition": "pro", "features": list(F.PRO_FEATURES),
               "machine_id": None, "issued_at": "2026-01-01T00:00:00Z",
               "expires_at": None}
    sig = priv.sign(canonical(payload))
    blob = encode_activation(payload, base64.b64encode(sig).decode())

    r = client.post("/api/license/activate", data={"key": blob})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert client.get("/api/license/status").json()["active"] is True
    # now unlocked
    assert client.post(f"/drafts/{draft.id}/styled-html",
                       data={"platform": "wechat"}).status_code == 200


def test_draft_edit_and_save(tmp_path):
    client, store, _ = make_client(tmp_path)
    art, draft = seed(store)
    # Page serves the SPA shell; content comes from the JSON API.
    data = client.get(f"/api/draft/{draft.id}").json()
    assert data["ok"]
    assert "正文内容" in data["body_md"]
    assert "可疑说法X" in data["flagged_claims"]  # flagged claims present

    r2 = client.post(f"/drafts/{draft.id}", data={
        "title_candidates": "新标题A\n新标题B",
        "body_md": "## 改写后的正文\n更好了",
        "status": "approved"}, follow_redirects=False)
    assert r2.status_code == 303
    reloaded = store.read_draft_body(draft.id)
    assert "改写后的正文" in reloaded["body_md"]
    assert reloaded["title_candidates"] == ["新标题A", "新标题B"]
    assert store.get_draft(draft.id)["status"] == "approved"


def test_draft_save_preserves_from_page(tmp_path):
    """POST /drafts/{id} preserves the from_page form field in the redirect URL."""
    client, store, _ = make_client(tmp_path)
    art, draft = seed(store)

    # Save with from_page=archive → redirect should include ?from=archive
    r = client.post(f"/drafts/{draft.id}", data={
        "title_candidates": "新标题",
        "body_md": "正文",
        "status": "reviewing",
        "from_page": "archive"}, follow_redirects=False)
    assert r.status_code == 303
    assert "?from=archive" in r.headers["location"]

    # Save with from_page=drafts
    r2 = client.post(f"/drafts/{draft.id}", data={
        "title_candidates": "标题2",
        "body_md": "正文2",
        "status": "approved",
        "from_page": "drafts"}, follow_redirects=False)
    assert r2.status_code == 303
    assert "?from=drafts" in r2.headers["location"]

    # Save WITHOUT from_page → no ?from= in redirect
    r3 = client.post(f"/drafts/{draft.id}", data={
        "title_candidates": "标题3",
        "body_md": "正文3"}, follow_redirects=False)
    assert r3.status_code == 303
    assert "?from=" not in r3.headers["location"]


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
    # seeded article already has a draft -> present in draft_map
    data = client.get("/api/archive").json()
    assert str(art.id) in data["draft_map"]


def test_draft_edit_has_tabs(tmp_path):
    client, store, _ = make_client(tmp_path)
    _art, draft = seed(store)
    # Page route serves the SPA shell (React renders the tabs client-side).
    r = client.get(f"/drafts/{draft.id}/edit")
    assert r.status_code == 200
    assert 'id="root"' in r.text
    # The draft data API backs the editor.
    data = client.get(f"/api/draft/{draft.id}").json()
    assert data["ok"] and "statuses" in data


def test_archive_view_renders_original(tmp_path):
    # The page route (/archive/{id}/view) serves the React SPA shell; the
    # article content is delivered by the JSON API the SPA fetches.
    client, store, _ = make_client(tmp_path)
    art, _draft = seed(store)
    assert client.get(f"/archive/{art.id}/view").status_code == 200  # SPA shell
    data = client.get(f"/api/archive/{art.id}/view").json()
    assert data["ok"] is True
    assert data["article"]["title"] == "GPT-5 breakthrough"
    assert "content_md" in data


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

    # Rewrite is async (background thread + progress polling).
    # The POST returns immediately with "started".
    r = client.post(f"/archive/{art.id}/rewrite")
    assert r.status_code == 200
    data = r.json()
    assert data["started"] is True

    # Poll until rewrite completes (MockProvider is instant, so this is fast)
    import time
    for _ in range(150):  # up to ~15s (cold-start imports can be slow)
        status = client.get(f"/api/rewrite-status?article_id={art.id}").json()
        if status.get("done"):
            break
        time.sleep(0.1)
    else:
        raise AssertionError("Rewrite did not complete in time")

    # A draft now exists for this article
    draft = store.get_draft_for_article(art.id)
    assert draft is not None
    assert status.get("draft_id") == draft["id"]

    # Rewrite again should overwrite (still exactly one draft)
    r2 = client.post(f"/archive/{art.id}/rewrite")
    assert r2.json()["started"] is True
    for _ in range(150):
        s2 = client.get(f"/api/rewrite-status?article_id={art.id}").json()
        if s2.get("done"):
            break
        time.sleep(0.1)
    drafts_for_article = [d for d in store.list_drafts()
               if d["article_id"] == art.id]
    assert len(drafts_for_article) == 1


def test_archive_rewrite_promotion_footer_no_mechanical_append(tmp_path):
    """The rewrite worker does NOT mechanically append the static promotion
    footer to draft body_md (the LLM handles it via prompt)."""
    client, store, _ = make_client(tmp_path)

    # Configure promotion footer in DB
    footer = "关注我们，获取更多前沿资讯"
    store.set_setting("promotion_footer", footer)

    art = store.save_article(Article(
        title="Test", content_md="# Body\nfacts",
        url="https://x.com/d", source_name="TestSource", source_type="rss",
        published_at=datetime(2026, 2, 1, tzinfo=timezone.utc), images=[],
        raw_summary=None, fetched_at=datetime(2026, 2, 2, tzinfo=timezone.utc),
        topic="AI"))

    # Trigger rewrite
    r = client.post(f"/archive/{art.id}/rewrite")
    assert r.status_code == 200
    assert r.json()["started"] is True

    # Poll until done
    import time
    for _ in range(150):  # up to ~15s (cold-start imports can be slow)
        status = client.get(f"/api/rewrite-status?article_id={art.id}").json()
        if status.get("done"):
            break
        time.sleep(0.1)
    else:
        raise AssertionError("Rewrite did not complete in time")

    # Read saved draft body
    draft = store.get_draft_for_article(art.id)
    assert draft is not None
    body = store.read_draft_body(draft["id"])
    body_md = body.get("body_md", "")

    # The mock LLM echoes the prompt (which includes the footer text once).
    # Without the old server.py mechanical append, the footer should NOT
    # appear a second time as a standalone block after the source attribution.
    assert body_md.count(footer) <= 1, (
        f"Promotion footer appears {body_md.count(footer)} times in body_md "
        f"(expected at most 1 — from the prompt echo only). "
        f"This means the worker mechanically appended it."
    )
    # Verify the source attribution is the last section, not a duplicated footer
    info_idx = body_md.rfind("**信息来源**")
    if info_idx != -1:
        after_info = body_md[info_idx:]
        assert "关注我们" not in after_info.replace(
            "关注我们", "", 1), (
            "Promotion footer found after **信息来源** — worker still "
            "mechanically appending it.")


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
    # Page serves the SPA shell; settings values come from the JSON API.
    r = client.get("/settings")
    assert r.status_code == 200
    assert 'id="root"' in r.text
    data = client.get("/api/settings").json()
    assert "llm_provider" in data and "rewrite_styles" in data


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


def test_apply_template_unknown_template_404(tmp_path):
    """apply-template rejects an unknown template id without starting a task."""
    client, store, _ = make_client(tmp_path)
    _, draft = seed(store)
    r = client.post(f"/api/draft/{draft.id}/apply-template",
                    data={"template_id": "__no_such_template__"})
    assert r.status_code == 404
    assert r.json()["error"] == "模板不存在"


def test_apply_template_missing_draft_404(tmp_path):
    """apply-template on a non-existent draft returns 404, not 500."""
    client, store, _ = make_client(tmp_path)
    r = client.post("/api/draft/999999/apply-template",
                    data={"template_id": "whatever"})
    assert r.status_code == 404


def test_rewrite_preserves_title_not_body(tmp_path):
    """Regression guard: an Agent/template rewrite must keep the draft's title
    and never dump the article body into title_cn.

    Exercises the shared _agent_run_background path (used by both Agent-edit and
    套用模板) via the apply-template endpoint, which runs on MockProvider in
    tests. Previously, when the rewritten output had no title_cn front-matter,
    the whole body was written into title_cn.
    """
    import time
    client, store, _ = make_client(tmp_path)          # dev license → not gated
    _, draft = seed(store)
    store.update_draft_body(draft.id, title_candidates=["原标题"],
                            body_md="# 原正文\n第一段原始内容，用于验证不会跑进标题。",
                            status="drafted", title_cn="我的中文标题")

    tpls = client.get("/api/editor/templates").json()["templates"]
    assert tpls, "expected builtin templates"
    r = client.post(f"/api/draft/{draft.id}/apply-template",
                    data={"template_id": tpls[0]["id"]})
    assert r.status_code == 200 and r.json().get("running") is True

    status = None
    for _ in range(200):                               # MockProvider is instant
        status = client.get(
            f"/api/draft/{draft.id}/agent-status").json()
        if status.get("status") in ("completed", "error"):
            break
        time.sleep(0.1)
    else:
        raise AssertionError("apply-template did not finish in time")
    assert status["status"] == "completed", status

    row = store.get_draft(draft.id)
    body_now = store.read_draft_body(draft.id).get("body_md", "") or ""
    # Title must be preserved — NOT replaced by (or containing) the body.
    assert row["title_cn"] == "我的中文标题"
    assert body_now and row["title_cn"] != body_now


def test_sadtalker_setup_stream_uses_standard_sse(tmp_path, monkeypatch):
    from app.video import sadtalker_setup as st

    client, _, _ = make_client(tmp_path)
    monkeypatch.setattr(st, "setup_status", lambda _: {
        "ready": False, "gpu_ok": True, "runtime_ok": False,
        "models_ok": True, "smoke_ok": False, "reason": "runtime missing",
    })

    def fake_setup(data_dir, *, mirror, proxy, progress_cb, cancel_event):
        assert mirror is True
        assert proxy is None
        progress_cb("下载中", 0.5)
        return {
            "ok": True,
            "message": "安装完成",
            "sadtalker_dir": "",
            "sadtalker_python": "",
        }

    monkeypatch.setattr(st, "run_setup", fake_setup)
    r = client.post("/api/sadtalker/setup", data={"mirror": "true"})
    assert r.status_code == 200
    assert "event: progress\ndata: " in r.text
    assert "event: done\ndata: " in r.text
    assert 'progress: {"message"' not in r.text


def test_sadtalker_status_requires_runtime_and_smoke(tmp_path, monkeypatch):
    from app.video import sadtalker_setup as st

    client, _, _ = make_client(tmp_path)
    expected = {
        "ready": False, "gpu_ok": True, "gpu_name": "Test GPU",
        "runtime_ok": False, "models_ok": True, "smoke_ok": False,
        "reason": "尚未安装 SadTalker runtime",
        "runtime_dir": str(tmp_path / "runtimes" / "sadtalker"),
        "models_dir": str(tmp_path / "models" / "sadtalker"),
        "files": [],
    }
    monkeypatch.setattr(st, "setup_status", lambda _: expected)

    response = client.get("/api/sadtalker/status")

    assert response.status_code == 200
    assert response.json() == expected


def test_cosyvoice_setup_stream_and_status(tmp_path, monkeypatch):
    from app import cosyvoice_install as cv

    client, _, _ = make_client(tmp_path)
    expected = {
        "ready": False, "gpu_ok": True, "gpu_name": "Test GPU",
        "runtime_ok": False, "models_ok": False, "smoke_ok": False,
        "reason": "runtime missing",
        "runtime_dir": str(tmp_path / "runtimes" / "cosyvoice"),
        "models_dir": str(
            tmp_path / "models" / "cosyvoice" / "CosyVoice2-0.5B"),
    }
    monkeypatch.setattr(cv, "setup_status", lambda _: expected)

    def fake_setup(data_dir, *, proxy, progress_cb, cancel_event):
        assert data_dir == tmp_path
        assert proxy is None
        progress_cb("下载中", .5)
        return {**expected, "ok": True, "ready": True, "message": "安装完成"}

    monkeypatch.setattr(cv, "run_setup", fake_setup)
    assert client.get("/api/cosyvoice/status").json() == expected
    response = client.post("/api/cosyvoice/setup")
    assert response.status_code == 200
    assert "event: progress\ndata: " in response.text
    assert "event: done\ndata: " in response.text


def test_capabilities_uses_managed_runtime_contract(tmp_path, monkeypatch):
    from app import cosyvoice_install as cv
    from app.video import sadtalker_setup as st

    client, _, _ = make_client(tmp_path)
    cosyvoice = {
        "ready": False, "supported": True, "asset_available": True,
        "installable": True, "platform": "windows-cuda",
        "accelerator": "CUDA", "reason": "尚未安装 CosyVoice runtime",
    }
    sadtalker = {"ready": False, "reason": "尚未安装 SadTalker runtime"}
    monkeypatch.setattr(cv, "setup_status", lambda _: cosyvoice)
    monkeypatch.setattr(st, "setup_status", lambda _: sadtalker)

    response = client.get("/api/capabilities?refresh=true")

    assert response.status_code == 200
    payload = response.json()
    assert payload["cosyvoice"] == {
        **cosyvoice, "available": False, "warning": ""}
    assert payload["sadtalker"] == {
        **sadtalker, "available": False, "warning": ""}
    assert payload["ffmpeg"] == {"ready": payload["ffmpeg"]["ready"]}
