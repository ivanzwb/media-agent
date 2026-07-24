from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.config import Config
from app.db import connect, init_db
from app.models import Article, Draft
from app.store import Store


def make_store(tmp_path) -> Store:
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    conn = connect(config.db_path)
    init_db(conn)
    return Store(conn, config)


def save_article(store: Store, index: int) -> Article:
    return store.save_article(Article(
        title=f"Source article {index}",
        content_md=f"facts {index}",
        url=f"https://example{index}.com/a",
        source_name=f"Source {index}",
        source_type="scrape",
        published_at=datetime.now(timezone.utc),
        images=[],
        raw_summary=None,
        fetched_at=datetime.now(timezone.utc),
        topic="AI",
    ))


def test_search_draft_metadata_and_article_links(tmp_path, monkeypatch):
    monkeypatch.setattr("app.store._web_image_for_title", lambda *args: None)
    store = make_store(tmp_path)
    articles = [save_article(store, index) for index in range(1, 4)]
    sources = [
        {"article_id": item.id, "title": item.title, "url": item.url,
         "source_name": item.source_name, "rank": index,
         "role": "primary" if index == 1 else "source"}
        for index, item in enumerate(articles, 1)
    ]
    saved = store.save_draft(Draft(
        article_id=articles[0].id,
        title_candidates=["综合稿"],
        body_md="## 正文\n事实 [[1]](https://example1.com/a)",
        topic="AI",
        source_url=articles[0].url,
        source_name=articles[0].source_name,
        origin="search_create",
        sources=sources,
        citations=[{"claim": "事实", "source_indexes": [1]}],
        search_meta={"queries": ["AI news"], "ref_count": 5},
    ))

    row = store.get_draft(saved.id)
    assert row["origin"] == "search_create"
    meta = store.read_draft_body(saved.id)
    assert meta["origin"] == "search_create"
    assert len(meta["sources"]) == 3
    assert meta["citations"][0]["source_indexes"] == [1]
    links = store.conn.execute(
        "SELECT article_id, rank, role FROM draft_articles "
        "WHERE draft_id=? ORDER BY rank", (saved.id,)).fetchall()
    assert [(row["article_id"], row["rank"], row["role"]) for row in links] == [
        (articles[0].id, 1, "primary"),
        (articles[1].id, 2, "source"),
        (articles[2].id, 3, "source"),
    ]


def test_legacy_draft_migration_defaults_to_rewrite(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY, title TEXT, url TEXT, source_name TEXT,
            source_type TEXT, topic TEXT, published_at TEXT, fetched_at TEXT,
            archive_path TEXT, fingerprint TEXT UNIQUE, status TEXT
        );
        CREATE TABLE drafts (
            id INTEGER PRIMARY KEY, article_id INTEGER NOT NULL,
            draft_path TEXT, cover_image TEXT, title_cn TEXT,
            status TEXT, updated_at TEXT
        );
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO drafts
            (id, article_id, status, updated_at)
            VALUES (1, 99, 'drafted', '2026-01-01T00:00:00+00:00');
    """)
    init_db(conn)
    row = conn.execute(
        "SELECT origin FROM drafts WHERE id=1").fetchone()
    assert row[0] == "rewrite"
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name='draft_articles'"
    ).fetchone()
