from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import Config
from app.db import connect, init_db
from app.models import Article, Draft
from app.pipeline.series_export import series_html, series_markdown
from app.store import Store


NAV_NOTE = (
    "> 本文是《强化学习》系列第 2 篇，共 2 篇。\n"
    "> 上一篇《第一章》\n\n"
)

BODY = (
    "## 小节标题\n"
    "正文第一段，带一个 [1] 角标。\n\n"
    "```python\n"
    "# 这行井号在代码块里，不是标题\n"
    "print(1)\n"
    "```\n"
)


def make_store(tmp_path, monkeypatch) -> Store:
    monkeypatch.setattr("app.store._web_image_for_title", lambda *args: None)
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    conn = connect(config.db_path)
    init_db(conn)
    return Store(conn, config)


def seed(store, *, chapters: int = 2, body: str = NAV_NOTE + BODY,
         knowledge_map: str = "- 基础\n  - MDP") -> int:
    series_id = store.create_series(
        title="强化学习", topic="强化学习", lang="zh", depth="beginner",
        parts=chapters, style_id=None, knowledge_map=knowledge_map)
    chapter_ids = store.add_chapters(series_id, [
        {"title": f"第 {index} 章", "search_queries": ["q"]}
        for index in range(1, chapters + 1)])
    for index in range(1, chapters + 1):
        article = store.save_article(Article(
            title=f"来源 {index}", content_md="facts",
            url=f"https://source.test/{index}", source_name=f"站点 {index}",
            source_type="scrape", published_at=datetime.now(timezone.utc),
            images=[], raw_summary=None, fetched_at=datetime.now(timezone.utc),
            topic="强化学习"))
        draft = store.save_draft(Draft(
            article_id=article.id, title_candidates=[f"第 {index} 章成稿"],
            body_md=body, topic="强化学习", source_url=article.url,
            source_name=article.source_name, origin="series",
            sources=[{"article_id": article.id, "title": f"来源 {index}",
                      "url": f"https://source.test/{index}",
                      "source_name": f"站点 {index}", "rank": 1,
                      "role": "primary"}],
            series_id=series_id, series_order=index,
            series_part_label=f"第 {index} 章"))
        store.update_chapter(chapter_ids[index - 1], status="done",
                             draft_id=draft.id)
    return series_id


def test_markdown_export_reads_as_one_book(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    series_id = seed(store)

    text = series_markdown(store, series_id)

    assert text.startswith("# 强化学习\n")
    assert "## 目录\n\n1. 第 1 章成稿\n2. 第 2 章成稿" in text
    assert "## 知识脉络" in text and "- 基础" in text
    assert "## 第 1 章 第 1 章成稿" in text
    assert "### 本章参考资料" in text
    assert "1. [来源 2](https://source.test/2) — 站点 2" in text


def test_export_drops_the_chapter_cross_links(tmp_path, monkeypatch):
    """A compiled book has a table of contents; the nav block is noise in it."""
    store = make_store(tmp_path, monkeypatch)
    series_id = seed(store)

    text = series_markdown(store, series_id)

    assert "上一篇" not in text
    assert "本文是《强化学习》系列" not in text
    assert "正文第一段" in text


def test_export_keeps_chapter_headings_under_the_chapter(
        tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    series_id = seed(store)

    text = series_markdown(store, series_id)

    # The chapter owns level two, so its own headings move one level down.
    assert "### 小节标题" in text
    assert "\n## 小节标题" not in text
    # A comment inside a fence is not a heading and must survive untouched.
    assert "# 这行井号在代码块里，不是标题" in text


def test_html_export_stands_on_its_own(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    series_id = seed(store)

    document = series_html(store, series_id)

    assert document.startswith("<!DOCTYPE html>")
    assert "<title>强化学习</title>" in document
    assert "<style>" in document  # no stylesheet to fetch
    assert '<a href="#chapter-2">第 2 章成稿</a>' in document
    assert 'id="chapter-1"' in document
    assert "@media print" in document
    assert "<h3>本章参考资料</h3>" in document


def test_the_diagram_travels_as_a_list_where_nothing_can_draw_it(
        tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    series_id = seed(store, knowledge_map=(
        'graph TD\n'
        '    N1["强化学习"]\n'
        '    N2["基础概念"]\n'
        '    N3["马尔可夫决策过程"]\n'
        '    N4["核心算法"]\n'
        '    N1 --> N2\n'
        '    N2 --> N3\n'
        '    N1 --> N4'))

    text = series_markdown(store, series_id)

    assert "graph TD" not in text
    assert ("## 知识脉络\n\n"
            "- 强化学习\n"
            "  - 基础概念\n"
            "    - 马尔可夫决策过程\n"
            "  - 核心算法") in text


def test_export_needs_a_series_with_written_chapters(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    empty = store.create_series(
        title="空系列", topic="空", lang="zh", depth="beginner", parts=3,
        style_id=None)

    with pytest.raises(ValueError, match="还没有写成的章节"):
        series_markdown(store, empty)
    with pytest.raises(ValueError, match="系列不存在"):
        series_markdown(store, 9999)
