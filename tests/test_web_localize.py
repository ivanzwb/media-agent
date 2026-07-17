"""Tests for the draft media-localize endpoints (async job + status)."""

import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.config import Config
from app.db import connect, init_db
from app.models import Article, Draft
from app.store import Store
from app.web import server
from app.web.server import create_app


def make_client(tmp_path):
    cfg = Config(data_dir=tmp_path, llm_provider="mock", image_provider="mock")
    cfg.ensure_dirs()
    conn = connect(cfg.db_path)
    init_db(conn)
    store = Store(conn, cfg)
    app = create_app(cfg)
    app.state.license._dev = True
    return TestClient(app), store


def seed_draft_with_media(store):
    art = store.save_article(Article(
        title="Pic article", content_md="# Body",
        url="https://x.com/a", source_name="OpenAI", source_type="rss",
        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc), images=[],
        raw_summary=None, fetched_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        topic="AI"))
    body = "## Hook\n![pic](https://x.com/pic.png)\n正文内容"
    draft = store.save_draft(Draft(
        article_id=art.id, title_candidates=["原标题"], body_md=body,
        topic="AI", source_url=art.url, source_name=art.source_name))
    return draft


def test_localize_starts_and_reports_done(tmp_path, monkeypatch):
    client, store = make_client(tmp_path)
    draft = seed_draft_with_media(store)

    def fake_localize_article(art, config, progress=None,
                              download_images=True, download_videos=True,
                              relativize=True, **_kw):
        # Simulate the real machinery: emit progress + rewrite to local paths.
        if progress:
            progress("图片 1 张，视频 0 个")
            progress("  下载 #1 完成")
        # Draft endpoint requests absolute /media/ paths (relativize=False).
        new_url = "../../media/abc/pic.png" if relativize else "/media/abc/pic.png"
        art.content_md = art.content_md.replace("https://x.com/pic.png", new_url)
        art.images = ["/media/abc/pic.png"]
        return art

    monkeypatch.setattr(server, "localize_article", fake_localize_article)

    r = client.post(f"/api/draft/{draft.id}/localize")
    assert r.status_code == 200
    assert r.json()["started"] is True

    status = {}
    for _ in range(30):  # max 3 seconds
        status = client.get(f"/api/draft/{draft.id}/localize-status").json()
        if status.get("done"):
            break
        time.sleep(0.1)
    else:
        raise AssertionError("Localize did not complete in time")

    assert status["error"] is None
    assert status["running"] is False
    assert status["result"] == {"images": 1, "videos": 0}
    # Progress lines streamed to the log box.
    assert any("下载 #1" in line for line in status["logs"])

    # Body persisted with absolute local path (draft uses relativize=False),
    # and returned in status so the editor can refresh.
    reloaded = store.read_draft_body(draft.id)
    assert "/media/abc/pic.png" in reloaded["body_md"]
    assert "https://x.com/pic.png" not in reloaded["body_md"]
    assert status["body_md"] == reloaded["body_md"]


def test_localize_status_defaults_when_never_run(tmp_path):
    client, store = make_client(tmp_path)
    draft = seed_draft_with_media(store)
    s = client.get(f"/api/draft/{draft.id}/localize-status").json()
    assert s == {"running": False, "logs": [], "error": None,
                 "done": False, "result": None, "body_md": None}
