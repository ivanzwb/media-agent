from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.config import Config
from app.db import connect, init_db
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


def seed_series(tmp_path, *, status: str = "partial") -> int:
    config = Config(data_dir=tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    store = Store(conn, config)
    series_id = store.create_series(
        title="强化学习", topic="强化学习", lang="zh", depth="beginner",
        parts=2, style_id=None)
    chapter_ids = store.add_chapters(series_id, [
        {"title": "第一章", "scope": "打底", "search_queries": ["q1"],
         "prerequisites": []},
        {"title": "第二章", "scope": "展开", "search_queries": ["q2"],
         "prerequisites": ["第一章"]},
    ])
    store.update_chapter(chapter_ids[0], status="done", draft_id=7,
                         summary="开头段落")
    store.update_chapter(chapter_ids[1], status="failed", error="资料不足")
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
