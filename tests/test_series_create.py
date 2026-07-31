from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import Config
from app.db import connect, init_db
from app.models import Article
from app.pipeline.search_create import SearchCreateCancelled
from app.pipeline.series_create import (
    SeriesCreateOptions, generate_series_outline, probe_topic,
    run_series_create)
from app.sources.web_search import SearchHit
from app.store import Store


def article(index: int) -> Article:
    return Article(
        title=f"Article {index}",
        content_md=f"## Fact\nSource fact {index}. " * 30,
        url=f"https://example{index}.test/page",
        source_name=f"Source {index}",
        source_type="scrape",
        published_at=datetime.now(timezone.utc),
        images=[],
        raw_summary=None,
        fetched_at=datetime.now(timezone.utc),
        topic="RL",
    )


OUTLINE = (
    '{"chapters":['
    '{"title":"第一章","scope":"打底","search_queries":["q1"],'
    '"prerequisites":[]},'
    '{"title":"第二章","scope":"展开","search_queries":["q2"],'
    '"prerequisites":["第一章"]},'
    '{"title":"第三章","scope":"收束","search_queries":["q3"],'
    '"prerequisites":["第二章"]}]}'
)


class ScriptedProvider:
    """Answers by prompt shape, so chapter count never shifts the script."""

    def __init__(self, outline: str = OUTLINE):
        self.outline = outline
        self.prompts: list[str] = []

    def chat(self, messages, **opts) -> str:
        prompt = messages[-1].content
        self.prompts.append(prompt)
        if "事实核查员" in prompt:
            return '{"flagged_claims":[]}'
        if "系列文章提纲" in prompt:
            return self.outline
        if "title_candidates" in prompt:
            index = sum(1 for item in self.prompts if "title_candidates" in item)
            return (
                f'{{"title_candidates":["成稿 {index}"],'
                f'"body_md":"## 小节\\n这一章的开头段落。\\n\\n正文 [1] 与 [2]。",'
                '"citations":[]}'
            )
        if '"scores"' in prompt:
            return '{"scores":[{"index":0,"score":90},{"index":1,"score":80}]}'
        return "[0,1,2,3,4,5,6,7,8,9]"


def make_store(tmp_path) -> Store:
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    conn = connect(config.db_path)
    init_db(conn)
    return Store(conn, config)


def options(**overrides) -> SeriesCreateOptions:
    values = {
        "topic": "强化学习",
        "parts": 3,
        "ref_count": 5,
        "engines": ["duckduckgo"],
        "workers": 2,
    }
    values.update(overrides)
    return SeriesCreateOptions(**values)


def wire(monkeypatch, *, hits_for, articles_by_url, scrape_calls=None):
    """Route search and scrape through in-memory fixtures."""
    def fake_search(queries, opts, **kwargs):
        collected: list[SearchHit] = []
        for query in queries:
            collected.extend(hits_for(query))
        seen, unique = set(), []
        for item in collected:
            if item.url not in seen:
                seen.add(item.url)
                unique.append(item)
        return unique

    def fake_scrape(hits, opts, **kwargs):
        if scrape_calls is not None:
            scrape_calls.append([item.url for item in hits])
        return [articles_by_url[item.url] for item in hits
                if item.url in articles_by_url]

    monkeypatch.setattr(
        "app.pipeline.search_create.search_queries", fake_search)
    monkeypatch.setattr(
        "app.pipeline.series_create.search_queries", fake_search)
    monkeypatch.setattr(
        "app.pipeline.search_create.scrape_search_hits", fake_scrape)
    monkeypatch.setattr("app.store._web_image_for_title", lambda *args: None)


def pool(count: int) -> tuple[list[SearchHit], dict[str, Article]]:
    articles = [article(index) for index in range(1, count + 1)]
    hits = [SearchHit(row.title, row.url, f"摘要 {index}")
            for index, row in enumerate(articles, 1)]
    return hits, {row.url: row for row in articles}


def test_probe_topic_reads_titles_without_fetching_pages(monkeypatch):
    hits = [
        SearchHit("入门指南", "https://a.test/one", "  讲清楚了  基础  "),
        SearchHit("第二篇", "https://a.test/two", "同域第二篇"),
        SearchHit("第三篇", "https://a.test/three", "同域第三篇，应被丢弃"),
        SearchHit("另一站", "https://b.test/one", "另一个域名"),
    ]
    scrape_calls: list[list[str]] = []
    wire(monkeypatch, hits_for=lambda query: hits, articles_by_url={},
         scrape_calls=scrape_calls)

    stats: dict = {}
    grounding = probe_topic(options(), stats=stats)

    assert scrape_calls == []  # grounding must never fetch a page
    assert "入门指南：讲清楚了 基础" in grounding
    assert "另一站：另一个域名" in grounding
    assert "第三篇" not in grounding  # capped at two hits per domain
    assert stats["grounding_hits"] == 3


def test_outline_falls_back_to_topic_terms_when_queries_missing():
    provider = ScriptedProvider(
        '{"chapters":[{"title":"背景","scope":"","prerequisites":"入门"}]}')
    chapters = generate_series_outline(
        "强化学习", "", provider, parts=1, depth="intermediate")
    assert chapters[0]["search_queries"] == ["强化学习 背景"]
    assert chapters[0]["prerequisites"] == ["入门"]


def test_outline_rejects_unusable_model_output():
    provider = ScriptedProvider('{"chapters":[]}')
    with pytest.raises(ValueError, match="系列提纲生成失败"):
        generate_series_outline(
            "强化学习", "", provider, parts=3, depth="intermediate")


def test_series_writes_linked_drafts_in_order(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    wire(monkeypatch, hits_for=lambda query: hits,
         articles_by_url=articles_by_url)

    result = run_series_create(store, ScriptedProvider(), options())

    assert result["status"] == "done"
    assert len(result["draft_ids"]) == 3
    drafts = store.get_series_drafts(result["series_id"])
    assert [row["series_order"] for row in drafts] == [1, 2, 3]
    assert {row["origin"] for row in drafts} == {"series"}
    chapters = store.list_chapters(result["series_id"])
    assert [row["status"] for row in chapters] == ["done"] * 3
    assert all(row["draft_id"] for row in chapters)
    assert store.get_series(result["series_id"])["status"] == "done"

    body = store.read_draft_body(drafts[0]["id"])
    assert body["series_part_label"] == "第 1 章"
    assert body["search_meta"]["chapter"] == "第一章"


def test_chapters_never_reuse_an_earlier_chapters_sources(
        tmp_path, monkeypatch):
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    scrape_calls: list[list[str]] = []
    wire(monkeypatch, hits_for=lambda query: hits,
         articles_by_url=articles_by_url, scrape_calls=scrape_calls)

    result = run_series_create(store, ScriptedProvider(), options())

    drafts = store.get_series_drafts(result["series_id"])
    per_chapter = [
        {source["url"]
         for source in store.read_draft_body(row["id"])["sources"]}
        for row in drafts]
    assert sum(len(item) for item in per_chapter) == len(set().union(*per_chapter))
    # Reserved sources are dropped before the fetch, so the saving is real.
    # Pages chapter 1 merely looked at stay available: a page it ranked low is
    # not necessarily a poor fit for a later chapter.
    assert not per_chapter[0] & set(scrape_calls[1])


def test_starved_chapter_fails_alone_and_series_reports_partial(
        tmp_path, monkeypatch):
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    # The middle chapter is the narrow one: nothing matches its terms, and the
    # widened retry finds nothing either.
    wire(monkeypatch,
         hits_for=lambda query: [] if "q2" in query or "第二章" in query
         else hits,
         articles_by_url=articles_by_url)

    result = run_series_create(store, ScriptedProvider(), options())

    assert result["status"] == "partial"
    assert result["stats"]["chapters_done"] == 2
    assert result["stats"]["chapters_failed"] == 1
    chapters = store.list_chapters(result["series_id"])
    assert [row["status"] for row in chapters] == ["done", "failed", "done"]
    assert "不足 2 篇" in chapters[1]["error"]
    assert len(store.get_series_drafts(result["series_id"])) == 2
    assert store.get_series(result["series_id"])["status"] == "partial"


def test_widened_retry_rescues_a_chapter_the_outline_mistermed(
        tmp_path, monkeypatch):
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    wire(monkeypatch,
         hits_for=lambda query: hits if "强化学习" in query else [],
         articles_by_url=articles_by_url)

    result = run_series_create(store, ScriptedProvider(), options(parts=3))

    # None of the outline's own terms match; only the topic-widened retry does.
    assert result["stats"]["chapters_done"] == 3
    assert result["status"] == "done"


def test_cancel_keeps_the_chapters_already_written(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    wire(monkeypatch, hits_for=lambda query: hits,
         articles_by_url=articles_by_url)
    calls = {"count": 0}

    def should_stop() -> bool:
        calls["count"] += 1
        return calls["count"] > 12

    with pytest.raises(SearchCreateCancelled):
        run_series_create(
            store, ScriptedProvider(), options(), should_stop=should_stop)

    series = store.latest_series()
    assert series["status"] == "cancelled"
    statuses = [row["status"] for row in store.list_chapters(series["id"])]
    assert "done" in statuses
    assert "cancelled" in statuses
    assert store.get_series_drafts(series["id"])  # finished work survives


def test_later_chapters_receive_earlier_chapter_summaries(
        tmp_path, monkeypatch):
    store = make_store(tmp_path)
    hits, articles_by_url = pool(12)
    wire(monkeypatch, hits_for=lambda query: hits,
         articles_by_url=articles_by_url)
    provider = ScriptedProvider()

    run_series_create(store, provider, options())

    writes = [item for item in provider.prompts if "title_candidates" in item]
    assert "系列上下文" not in writes[0]
    assert "第 1 章《第一章》：这一章的开头段落。" in writes[1]
    assert "第 2 章《第二章》" in writes[2]


def test_options_reject_out_of_range_part_counts():
    with pytest.raises(ValueError, match="章节数必须在"):
        options(parts=2).validate()
    with pytest.raises(ValueError, match="章节数必须在"):
        options(parts=11).validate()


def test_chapter_search_ignores_the_recency_window():
    """Knowledge series cover settled material, not the last 30 days."""
    assert options().chapter_options().time_range_days is None
