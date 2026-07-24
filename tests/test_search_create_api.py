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


def wait_terminal(client: TestClient, timeout: float = 2.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get("/api/search-create/status").json()
        if not state["running"]:
            return state
        time.sleep(0.01)
    raise AssertionError("search-create did not reach a terminal state")


def valid_payload() -> dict:
    return {
        "topic": "AI agents",
        "lang": "bilingual",
        "time_range_days": 30,
        "ref_count": 5,
        "style_id": "deep-tech",
    }


def test_search_create_requires_pro(tmp_path):
    client = make_client(tmp_path, pro=False)
    response = client.post("/api/search-create", json=valid_payload())
    assert response.status_code == 403
    assert response.json()["upgrade"] is True


def test_search_create_validates_request(tmp_path):
    client = make_client(tmp_path, pro=True)
    response = client.post(
        "/api/search-create",
        json={**valid_payload(), "ref_count": 7})
    assert response.status_code == 400


def test_search_create_start_and_status(tmp_path, monkeypatch):
    def fake_run(store, provider, options, **kwargs):
        kwargs["progress"]({
            "stage": "synthesize", "detail": "正在综合",
            "current": 1, "total": 1, "stats": {"kept": 3},
        })
        return {"draft_id": 42, "stats": {"draft_id": 42, "kept": 3}}

    monkeypatch.setattr("app.web.server.run_search_create", fake_run)
    client = make_client(tmp_path, pro=True)
    response = client.post("/api/search-create", json=valid_payload())
    assert response.status_code == 200
    state = wait_terminal(client)
    assert state["status"] == "done"
    assert state["draft_id"] == 42
    assert state["stats"]["kept"] == 3


def test_search_create_single_flight_and_cancel(tmp_path, monkeypatch):
    def fake_run(store, provider, options, **kwargs):
        while not kwargs["should_stop"]():
            time.sleep(0.01)
        raise SearchCreateCancelled()

    monkeypatch.setattr("app.web.server.run_search_create", fake_run)
    client = make_client(tmp_path, pro=True)
    assert client.post(
        "/api/search-create", json=valid_payload()).status_code == 200
    assert client.post(
        "/api/search-create", json=valid_payload()).status_code == 409
    assert client.post("/api/search-create/cancel").status_code == 200
    state = wait_terminal(client)
    assert state["status"] == "cancelled"
    assert state["error"] is None


def test_draft_api_returns_search_source_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr("app.store._web_image_for_title", lambda *args: None)
    client = make_client(tmp_path, pro=True)
    config = Config(data_dir=tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    store = Store(conn, config)
    source = store.save_article(Article(
        title="Source", content_md="facts", url="https://source.test/a",
        source_name="Source", source_type="scrape",
        published_at=datetime.now(timezone.utc), images=[],
        raw_summary=None, fetched_at=datetime.now(timezone.utc), topic="AI"))
    draft = store.save_draft(Draft(
        article_id=source.id, title_candidates=["Search draft"],
        body_md="## body", topic="AI", source_url=source.url,
        source_name=source.source_name, origin="search_create",
        sources=[{"article_id": source.id, "title": source.title,
                  "url": source.url, "rank": 1, "role": "primary"}],
        citations=[{"claim": "facts", "source_indexes": [1]}],
        search_meta={"queries": ["AI"]},
    ))
    response = client.get(f"/api/draft/{draft.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["origin"] == "search_create"
    assert data["sources"][0]["url"] == source.url
    assert data["citations"][0]["source_indexes"] == [1]
    assert data["search_meta"]["queries"] == ["AI"]
