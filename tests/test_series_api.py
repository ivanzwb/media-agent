from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.config import Config
from app.db import connect, init_db
from app.models import Article, Draft
from app.pipeline.search_create import SearchCreateCancelled
from app.store import Store
from app.web.server import create_app


def make_client(tmp_path, *, pro: bool) -> TestClient:
    config = Config(data_dir=tmp_path, llm_provider="mock")
    config.ensure_dirs()
    feeds = tmp_path / "feeds.yaml"
    feeds.write_text("topics: []\nsources: []\n", encoding="utf-8")
    app = create_app(config, feeds_path=feeds)
    app.state.license._dev = pro
    return TestClient(app)


def wait_terminal(client: TestClient, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get("/api/series/status").json()
        if not state["running"]:
            return state
        time.sleep(0.01)
    raise AssertionError("series creation did not reach a terminal state")


def valid_payload() -> dict:
    return {
        "topic": "强化学习",
        "lang": "zh",
        "depth": "beginner",
        "parts": 4,
        "ref_count": 5,
        "style_id": "deep-tech",
        "engines": ["duckduckgo", "google"],
    }


def seed_series(tmp_path, *, status: str = "partial", written: bool = True,
                second_draft: bool = False) -> int:
    config = Config(data_dir=tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    store = Store(conn, config)
    series_id = store.create_series(
        title="强化学习", topic="强化学习", lang="zh", depth="beginner",
        parts=2, style_id=None, status=status)
    chapter_ids = store.add_chapters(series_id, [
        {"title": "第一章", "scope": "打底", "search_queries": ["q1"],
         "prerequisites": [], "preflight_hits": 9},
        {"title": "第二章", "scope": "展开", "search_queries": ["q2"],
         "prerequisites": ["第一章"], "preflight_hits": 1},
    ])
    if written:
        store.update_chapter(chapter_ids[0], status="done", draft_id=7,
                             summary="开头段落")
        store.update_chapter(
            chapter_ids[1],
            **({"status": "done", "draft_id": 8} if second_draft
               else {"status": "failed", "error": "资料不足"}))
    store.finish_series(series_id, status)
    return series_id


def test_series_create_requires_pro(tmp_path):
    client = make_client(tmp_path, pro=False)
    response = client.post("/api/series/create", json=valid_payload())
    assert response.status_code == 403
    assert response.json()["upgrade"] is True


def test_series_create_validates_request(tmp_path):
    client = make_client(tmp_path, pro=True)
    for bad in ({"parts": 2}, {"parts": 11}, {"depth": "expert"},
                {"engines": ["yandex"]}, {"topic": "  "}, {"ref_count": 7}):
        response = client.post(
            "/api/series/create", json={**valid_payload(), **bad})
        assert response.status_code == 400, bad


def test_series_create_start_and_status(tmp_path, monkeypatch):
    captured = {}

    def fake_run(store, provider, options, **kwargs):
        captured["options"] = options
        captured["kwargs"] = kwargs
        kwargs["progress"]({
            "stage": "write", "detail": "第 1/4 章：正在写作…",
            "current": 1, "total": 4, "stats": {"series_id": 1},
        })
        return {"series_id": 1, "status": "done", "draft_ids": [11],
                "stats": {"series_id": 1, "chapters_done": 4}}

    monkeypatch.setattr("app.web.server.run_series_create", fake_run)
    client = make_client(tmp_path, pro=True)
    assert client.post(
        "/api/series/create", json=valid_payload()).status_code == 200
    state = wait_terminal(client)
    assert state["status"] == "done"
    assert state["stats"]["chapters_done"] == 4
    assert state["request"]["depth"] == "beginner"
    assert state["request"]["engines"] == ["duckduckgo", "google"]
    assert captured["options"].parts == 4
    assert captured["options"].style_id == "deep-tech"
    assert captured["kwargs"]["seo_tags_enabled"] is True
    assert "最佳" in captured["kwargs"]["sensitive_words"]
    assert any("正在写作" in line for line in state["logs"])


def test_series_and_search_create_are_mutually_exclusive(
        tmp_path, monkeypatch):
    def blocking_run(store, provider, options, **kwargs):
        while not kwargs["should_stop"]():
            time.sleep(0.01)
        raise SearchCreateCancelled()

    monkeypatch.setattr("app.web.server.run_series_create", blocking_run)
    client = make_client(tmp_path, pro=True)
    assert client.post(
        "/api/series/create", json=valid_payload()).status_code == 200

    second = client.post("/api/series/create", json=valid_payload())
    assert second.status_code == 409
    # Both jobs compete for the same LLM quota, so one blocks the other.
    search = client.post("/api/search-create", json={
        "topic": "AI", "lang": "zh", "time_range_days": 30,
        "ref_count": 5, "engines": ["duckduckgo"]})
    assert search.status_code == 409
    assert "系列创作" in search.json()["error"]

    assert client.post("/api/series/cancel").status_code == 200
    state = wait_terminal(client)
    assert state["status"] == "cancelled"
    assert state["error"] is None


def test_series_cancel_without_a_running_job(tmp_path):
    client = make_client(tmp_path, pro=True)
    response = client.post("/api/series/cancel")
    assert response.status_code == 409


def test_cancel_after_restart_clears_stale_series(tmp_path):
    """A series run interrupted by a server restart leaves the series row
    stuck at 'running'. The status endpoint rebuilds from it, so cancel must
    mark the series cancelled instead of refusing with 409."""
    client = make_client(tmp_path, pro=True)
    config = Config(data_dir=tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    store = Store(conn, config)
    series_id = store.create_series(
        title="强化学习", topic="强化学习", lang="zh", depth="beginner",
        parts=2, style_id=None, status="planned")
    store.mark_series_running(series_id)
    conn.close()

    # A fresh app over the same data directory stands in for a restart.
    restarted = make_client(tmp_path, pro=True)
    state = restarted.get("/api/series/status").json()
    assert state["status"] == "running"

    response = restarted.post("/api/series/cancel")
    assert response.status_code == 200
    assert response.json()["cancel_requested"] is True

    conn = connect(config.db_path)
    try:
        row = Store(conn, config).get_series(series_id)
    finally:
        conn.close()
    assert row is not None
    assert row["status"] == "cancelled"

    state = restarted.get("/api/series/status").json()
    assert state["status"] == "cancelled"
    assert state["running"] is False


def test_status_rebuilds_chapter_progress_from_the_database(tmp_path):
    """A series outlives the process that started it."""
    make_client(tmp_path, pro=True)
    series_id = seed_series(tmp_path)

    # A fresh app over the same data directory stands in for a restart.
    restarted = make_client(tmp_path, pro=True)
    state = restarted.get("/api/series/status").json()
    assert state["running"] is False
    assert state["status"] == "partial"
    assert state["series_id"] == series_id
    assert state["series"]["chapters_done"] == 1
    assert state["series"]["chapters_failed"] == 1
    assert state["current"] == 1
    assert state["topic"] == "强化学习"


def test_draft_detail_carries_series_navigation(tmp_path, monkeypatch):
    monkeypatch.setattr("app.store._web_image_for_title", lambda *args: None)
    client = make_client(tmp_path, pro=True)
    config = Config(data_dir=tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    store = Store(conn, config)
    series_id = store.create_series(
        title="强化学习", topic="强化学习", lang="zh", depth="beginner",
        parts=2, style_id=None, knowledge_map="- 基础\n  - MDP")
    article = store.save_article(Article(
        title="Source", content_md="facts", url="https://source.test/a",
        source_name="Source", source_type="scrape",
        published_at=datetime.now(timezone.utc), images=[], raw_summary=None,
        fetched_at=datetime.now(timezone.utc), topic="强化学习"))
    draft = store.save_draft(Draft(
        article_id=article.id, title_candidates=["第一章成稿"],
        body_md="## 正文", topic="强化学习", source_url=article.url,
        source_name=article.source_name, origin="series",
        series_id=series_id, series_order=1, series_part_label="第 1 章",
        prerequisites=["入门"]))
    chapter_ids = store.add_chapters(series_id, [
        {"title": "第一章", "search_queries": ["q1"]},
        {"title": "第二章", "search_queries": ["q2"]},
    ])
    store.update_chapter(chapter_ids[0], status="done", draft_id=draft.id)
    store.update_chapter(chapter_ids[1], status="failed", error="资料不足")

    payload = client.get(f"/api/draft/{draft.id}").json()
    assert payload["origin"] == "series"
    nav = payload["series"]
    assert nav["id"] == series_id
    assert nav["order"] == 1
    assert nav["part_label"] == "第 1 章"
    assert nav["prerequisites"] == ["入门"]
    assert [item["draft_id"] for item in nav["chapters"]] == [draft.id, None]
    assert client.get(f"/api/series/{series_id}").json()[
        "series"]["knowledge_map"] == "- 基础\n  - MDP"


def test_draft_detail_has_no_series_block_for_a_plain_draft(
        tmp_path, monkeypatch):
    monkeypatch.setattr("app.store._web_image_for_title", lambda *args: None)
    client = make_client(tmp_path, pro=True)
    config = Config(data_dir=tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    store = Store(conn, config)
    article = store.save_article(Article(
        title="Source", content_md="facts", url="https://source.test/b",
        source_name="Source", source_type="scrape",
        published_at=datetime.now(timezone.utc), images=[], raw_summary=None,
        fetched_at=datetime.now(timezone.utc), topic="AI"))
    draft = store.save_draft(Draft(
        article_id=article.id, title_candidates=["普通草稿"], body_md="## 正文",
        topic="AI", source_url=article.url, source_name=article.source_name))

    assert client.get(f"/api/draft/{draft.id}").json()["series"] is None


def test_series_detail_and_list(tmp_path):
    client = make_client(tmp_path, pro=True)
    series_id = seed_series(tmp_path, status="done")

    listing = client.get("/api/series").json()
    assert [item["id"] for item in listing["series"]] == [series_id]

    detail = client.get(f"/api/series/{series_id}").json()["series"]
    assert detail["status"] == "done"
    chapters = detail["chapters"]
    assert [item["order"] for item in chapters] == [1, 2]
    assert chapters[0]["draft_id"] == 7
    assert chapters[1]["error"] == "资料不足"
    assert chapters[1]["prerequisites"] == ["第一章"]
    assert chapters[0]["search_queries"] == ["q1"]

    assert client.get("/api/series/9999").status_code == 404
    assert client.get("/api/series/9999/drafts").status_code == 404


def test_plan_leaves_the_series_waiting_for_approval(tmp_path, monkeypatch):
    captured = {}

    def fake_plan(store, provider, options, **kwargs):
        captured["options"] = options
        captured["status"] = kwargs.get("status")
        kwargs["progress"]({
            "stage": "outline", "detail": "提纲已生成，共 4 章",
            "current": 0, "total": 4, "stats": {"series_id": 1},
        })
        series_id = store.create_series(
            title=options.topic, topic=options.topic, lang=options.lang,
            depth=options.depth, parts=2, style_id=None, status="planned")
        store.add_chapters(series_id, [
            {"title": "第一章", "search_queries": ["q1"], "preflight_hits": 9},
            {"title": "第二章", "search_queries": ["q2"], "preflight_hits": 1},
        ])
        return series_id, [], {}

    monkeypatch.setattr("app.web.server.plan_series", fake_plan)
    client = make_client(tmp_path, pro=True)
    assert client.post(
        "/api/series/plan", json=valid_payload()).status_code == 200
    state = wait_terminal(client)

    assert captured["status"] == "planned"
    assert captured["options"].parts == 4
    assert state["status"] == "planned"
    chapters = state["series"]["chapters"]
    # The per-chapter counts travel to the UI, which is where the outline is
    # judged worth running or worth fixing.
    assert [item["preflight_hits"] for item in chapters] == [9, 1]
    assert [item["status"] for item in chapters] == ["pending", "pending"]


def test_a_request_without_a_chapter_count_leaves_it_to_the_outline(
        tmp_path, monkeypatch):
    captured = {}

    def fake_plan(store, provider, options, **kwargs):
        captured["options"] = options
        series_id = store.create_series(
            title=options.topic, topic=options.topic, lang=options.lang,
            depth=options.depth, parts=6, style_id=None, status="planned")
        return series_id, [], {}

    monkeypatch.setattr("app.web.server.plan_series", fake_plan)
    client = make_client(tmp_path, pro=True)
    payload = {key: value for key, value in valid_payload().items()
               if key != "parts"}

    assert client.post("/api/series/plan", json=payload).status_code == 200
    state = wait_terminal(client)

    assert captured["options"].parts is None
    assert state["request"]["parts"] is None
    assert state["series"]["parts"] == 6  # whatever the outline came back with


def test_plan_requires_pro(tmp_path):
    client = make_client(tmp_path, pro=False)
    assert client.post(
        "/api/series/plan", json=valid_payload()).status_code == 403


def test_outline_edits_replace_the_chapters(tmp_path):
    client = make_client(tmp_path, pro=True)
    series_id = seed_series(tmp_path, status="planned", written=False)

    response = client.put(f"/api/series/{series_id}/outline", json={
        "chapters": [
            {"title": "改过的开头", "scope": "打底",
             "search_queries": [" 改过的词 ", ""], "prerequisites": []},
            {"title": "第二章", "search_queries": ["q2"]},
            {"title": "补的一章", "search_queries": []},
        ],
    })

    assert response.status_code == 200
    chapters = response.json()["series"]["chapters"]
    assert [item["order"] for item in chapters] == [1, 2, 3]
    assert [item["title"] for item in chapters] == [
        "改过的开头", "第二章", "补的一章"]
    assert chapters[0]["search_queries"] == ["改过的词"]
    # A chapter left without terms would search for nothing at all.
    assert chapters[2]["search_queries"] == ["补的一章"]
    assert response.json()["series"]["parts"] == 3
    # The preflight count survives a retitle but not a change of terms.
    assert [item["preflight_hits"] for item in chapters] == [None, 1, None]


def test_outline_edits_are_validated(tmp_path):
    client = make_client(tmp_path, pro=True)
    series_id = seed_series(tmp_path, status="planned", written=False)
    chapter = {"title": "一章", "search_queries": ["q"]}
    for bad in ({"chapters": [chapter] * 2}, {"chapters": [chapter] * 11},
                {"chapters": [{"title": "  "}]}, {"chapters": "nope"},
                {"chapters": [{"title": "x", "search_queries": "q"}]}):
        assert client.put(
            f"/api/series/{series_id}/outline", json=bad).status_code == 400, bad
    assert client.put(
        "/api/series/9999/outline",
        json={"chapters": [chapter] * 3}).status_code == 404


def test_outline_is_frozen_once_a_chapter_is_written(tmp_path):
    client = make_client(tmp_path, pro=True)
    series_id = seed_series(tmp_path)  # chapter 1 already has a draft

    response = client.put(f"/api/series/{series_id}/outline", json={
        "chapters": [{"title": f"第 {index} 章", "search_queries": ["q"]}
                     for index in range(1, 4)],
    })
    assert response.status_code == 409
    assert "提纲" in response.json()["error"]


def test_run_writes_the_approved_outline(tmp_path, monkeypatch):
    captured = {}

    def fake_run(store, provider, series_id, **kwargs):
        captured["series_id"] = series_id
        captured["kwargs"] = kwargs
        return {"series_id": series_id, "status": "done", "draft_ids": [31],
                "stats": {"series_id": series_id, "chapters_done": 2}}

    monkeypatch.setattr("app.web.server.run_series_chapters", fake_run)
    client = make_client(tmp_path, pro=True)
    series_id = seed_series(tmp_path, status="planned", written=False)

    assert client.post(f"/api/series/{series_id}/run").status_code == 200
    state = wait_terminal(client)
    assert state["status"] == "done"
    assert captured["series_id"] == series_id
    assert captured["kwargs"]["seo_tags_enabled"] is True


def test_run_rejects_a_series_with_nothing_left_to_write(
        tmp_path, monkeypatch):
    monkeypatch.setattr("app.store._web_image_for_title", lambda *args: None)
    client = make_client(tmp_path, pro=True)
    series_id = seed_series(tmp_path, status="done", second_draft=True)

    response = client.post(f"/api/series/{series_id}/run")
    assert response.status_code == 409
    assert client.post("/api/series/9999/run").status_code == 404


def test_series_export_downloads_one_document(tmp_path, monkeypatch):
    monkeypatch.setattr("app.store._web_image_for_title", lambda *args: None)
    client = make_client(tmp_path, pro=True)
    config = Config(data_dir=tmp_path)
    store = Store(connect(config.db_path), config)
    series_id = store.create_series(
        title="强化学习", topic="强化学习", lang="zh", depth="beginner",
        parts=1, style_id=None)
    article = store.save_article(Article(
        title="Source", content_md="facts", url="https://source.test/c",
        source_name="Source", source_type="scrape",
        published_at=datetime.now(timezone.utc), images=[], raw_summary=None,
        fetched_at=datetime.now(timezone.utc), topic="强化学习"))
    draft = store.save_draft(Draft(
        article_id=article.id, title_candidates=["第一章成稿"],
        body_md="## 小节\n正文。", topic="强化学习", source_url=article.url,
        source_name=article.source_name, origin="series",
        series_id=series_id, series_order=1, series_part_label="第 1 章"))
    chapter_ids = store.add_chapters(
        series_id, [{"title": "第一章", "search_queries": ["q"]}])
    store.update_chapter(chapter_ids[0], status="done", draft_id=draft.id)

    markdown = client.get(f"/api/series/{series_id}/export?format=md")
    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert "attachment;" in markdown.headers["content-disposition"]
    assert markdown.text.startswith("# 强化学习")

    page = client.get(f"/api/series/{series_id}/export?format=html")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.text.startswith("<!DOCTYPE html>")


def test_series_export_rejects_what_it_cannot_produce(tmp_path):
    client = make_client(tmp_path, pro=True)
    series_id = seed_series(tmp_path, status="planned", written=False)

    assert client.get(
        f"/api/series/{series_id}/export?format=pdf").status_code == 400
    assert client.get("/api/series/9999/export").status_code == 404
    # Planned but unwritten: there is nothing to compile yet.
    nothing = client.get(f"/api/series/{series_id}/export")
    assert nothing.status_code == 409
    assert "章节" in nothing.json()["error"]


def test_series_export_requires_pro(tmp_path):
    client = make_client(tmp_path, pro=False)
    series_id = seed_series(tmp_path)
    assert client.get(
        f"/api/series/{series_id}/export").status_code == 403


def retry_url(series_id: int, chapter_id: int) -> str:
    return f"/api/series/{series_id}/chapters/{chapter_id}/retry"


def chapter_ids_of(client: TestClient, series_id: int) -> list[int]:
    return [item["id"] for item in
            client.get(f"/api/series/{series_id}").json()["series"]["chapters"]]


def test_chapter_retry_runs_only_that_chapter(tmp_path, monkeypatch):
    captured = {}

    def fake_retry(store, provider, series_id, chapter_id, **kwargs):
        captured["target"] = (series_id, chapter_id)
        captured["kwargs"] = kwargs
        kwargs["progress"]({
            "stage": "write", "detail": "第 2 章：正在写作…",
            "current": 1, "total": 1, "stats": {"series_id": series_id},
        })
        return {"series_id": series_id, "status": "done", "draft_ids": [21],
                "stats": {"series_id": series_id, "chapters_done": 1}}

    monkeypatch.setattr("app.web.server.retry_chapter", fake_retry)
    client = make_client(tmp_path, pro=True)
    series_id = seed_series(tmp_path)
    failed = chapter_ids_of(client, series_id)[1]

    assert client.post(retry_url(series_id, failed)).status_code == 200
    state = wait_terminal(client)
    assert state["status"] == "done"
    assert captured["target"] == (series_id, failed)
    assert captured["kwargs"]["seo_tags_enabled"] is True
    assert "最佳" in captured["kwargs"]["sensitive_words"]
    assert any("正在写作" in line for line in state["logs"])


def test_chapter_retry_requires_pro(tmp_path):
    client = make_client(tmp_path, pro=False)
    series_id = seed_series(tmp_path)
    assert client.post(retry_url(series_id, 1)).status_code == 403


def test_chapter_retry_refuses_a_chapter_that_already_has_a_draft(tmp_path):
    client = make_client(tmp_path, pro=True)
    series_id = seed_series(tmp_path)
    written = chapter_ids_of(client, series_id)[0]

    response = client.post(retry_url(series_id, written))
    assert response.status_code == 409
    assert "草稿编辑页" in response.json()["error"]


def test_chapter_retry_rejects_unknown_targets(tmp_path):
    client = make_client(tmp_path, pro=True)
    series_id = seed_series(tmp_path)
    assert client.post(retry_url(9999, 1)).status_code == 404
    assert client.post(retry_url(series_id, 9999)).status_code == 404


def test_chapter_retry_waits_for_a_running_job(tmp_path, monkeypatch):
    def blocking_run(store, provider, options, **kwargs):
        while not kwargs["should_stop"]():
            time.sleep(0.01)
        raise SearchCreateCancelled()

    monkeypatch.setattr("app.web.server.run_series_create", blocking_run)
    client = make_client(tmp_path, pro=True)
    series_id = seed_series(tmp_path)
    failed = chapter_ids_of(client, series_id)[1]
    assert client.post(
        "/api/series/create", json=valid_payload()).status_code == 200

    response = client.post(retry_url(series_id, failed))
    assert response.status_code == 409

    assert client.post("/api/series/cancel").status_code == 200
    wait_terminal(client)


def test_series_delete_removes_series_and_its_drafts(tmp_path, monkeypatch):
    monkeypatch.setattr("app.store._web_image_for_title", lambda *args: None)
    client = make_client(tmp_path, pro=True)
    config = Config(data_dir=tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    store = Store(conn, config)
    series_id = store.create_series(
        title="强化学习", topic="强化学习", lang="zh", depth="beginner",
        parts=2, style_id=None)
    article = store.save_article(Article(
        title="Source", content_md="facts", url="https://source.test/c",
        source_name="Source", source_type="scrape",
        published_at=datetime.now(timezone.utc), images=[], raw_summary=None,
        fetched_at=datetime.now(timezone.utc), topic="强化学习"))
    draft = store.save_draft(Draft(
        article_id=article.id, title_candidates=["第一章成稿"],
        body_md="## 正文", topic="强化学习", source_url=article.url,
        source_name=article.source_name, origin="series",
        series_id=series_id, series_order=1))
    draft_path = config.data_dir / draft.draft_path
    assert draft_path.exists()
    chapter_ids = store.add_chapters(series_id, [
        {"title": "第一章", "search_queries": ["q1"]},
        {"title": "第二章", "search_queries": ["q2"]},
    ])
    store.update_chapter(chapter_ids[0], status="done", draft_id=draft.id)
    conn.close()

    response = client.delete(f"/api/series/{series_id}")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "deleted_drafts": 1}

    assert client.get(f"/api/series/{series_id}").status_code == 404
    assert client.get(f"/api/series/{series_id}/drafts").status_code == 404

    conn = connect(config.db_path)
    try:
        fresh = Store(conn, config)
        assert fresh.get_series(series_id) is None
        assert fresh.get_draft(draft.id) is None
        chapters = conn.execute(
            "SELECT COUNT(*) AS n FROM series_chapters WHERE series_id=?",
            (series_id,)).fetchone()
        assert chapters["n"] == 0
    finally:
        conn.close()
    assert not draft_path.exists()


def test_series_delete_requires_pro(tmp_path):
    client = make_client(tmp_path, pro=False)
    series_id = seed_series(tmp_path)
    assert client.delete(f"/api/series/{series_id}").status_code == 403


def test_series_delete_rejects_unknown_series(tmp_path):
    client = make_client(tmp_path, pro=True)
    assert client.delete("/api/series/9999").status_code == 404


def test_series_delete_refuses_a_running_series(tmp_path, monkeypatch):
    def blocking_run(store, provider, options, **kwargs):
        kwargs["progress"]({
            "stage": "write", "detail": "第 1/2 章：正在写作…",
            "current": 1, "total": 2, "stats": {"series_id": 1},
        })
        while not kwargs["should_stop"]():
            time.sleep(0.01)
        raise SearchCreateCancelled()

    monkeypatch.setattr("app.web.server.run_series_create", blocking_run)
    client = make_client(tmp_path, pro=True)
    seed_series(tmp_path)  # series id 1 exists in the DB
    assert client.post(
        "/api/series/create", json=valid_payload()).status_code == 200

    deadline = time.time() + 3.0
    while time.time() < deadline:
        state = client.get("/api/series/status").json()
        # The status endpoint falls back to the latest series row while the
        # in-memory series_id is still None, so wait for the job's own
        # progress event instead of the reported series id.
        if state["stage"] == "write" and state["series_id"] == 1:
            break
        time.sleep(0.01)

    response = client.delete("/api/series/1")
    assert response.status_code == 409
    assert "正在生成" in response.json()["error"]

    assert client.post("/api/series/cancel").status_code == 200
    wait_terminal(client)
