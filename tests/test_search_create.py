from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import Config
from app.db import connect, init_db
from app.llm.providers.mock import MockProvider
from app.models import Article
from app.pipeline.search_create import (
    SearchCreateCancelled, SearchCreateOptions, expand_search_queries,
    rank_by_topic, run_search_create, scrape_search_hits)
from app.pipeline.synthesizer import synthesize
from app.sources.web_search import (
    SearchHit, merge_search_hits, normalize_search_url, search_query)
from app.store import Store


def article(index: int, *, days_old: int = 1) -> Article:
    return Article(
        title=f"Article {index}",
        content_md=f"## Fact\nSource fact {index}. " * 30,
        url=f"https://example{index}.com/news",
        source_name=f"Source {index}",
        source_type="scrape",
        published_at=datetime.now(timezone.utc) - timedelta(days=days_old),
        images=[],
        raw_summary=None,
        fetched_at=datetime.now(timezone.utc),
        topic="AI",
        id=index,
    )


def test_search_url_and_cross_query_dedup():
    assert normalize_search_url(
        "HTTPS://Example.com/a/?utm_source=x#part") == "https://example.com/a"
    merged = merge_search_hits([
        [SearchHit("A", "https://example.com/a?utm_source=x")],
        [SearchHit("A2", "https://example.com/a#section"),
         SearchHit("B", "https://example.com/b")],
    ])
    assert [hit.url for hit in merged] == [
        "https://example.com/a", "https://example.com/b"]


def test_ddgs_failure_falls_back(monkeypatch):
    monkeypatch.setattr(
        "app.sources.web_search.search_ddgs_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(
        "app.sources.web_search.search_web",
        lambda *args, **kwargs: [
            "https://fallback.test/a?utm_campaign=x",
            "https://fallback.test/a",
        ])
    hits = search_query("topic")
    assert len(hits) == 2  # fallback preserves discovery output; merge removes it
    assert merge_search_hits([hits]) == [
        SearchHit("https://fallback.test/a?utm_campaign=x",
                  "https://fallback.test/a", "")]


def test_query_expansion_parses_json_and_has_fallback():
    provider = MockProvider(['{"queries":["AI research","AI news","AI news"]}'])
    assert expand_search_queries("AI", "en", provider) == [
        "AI research", "AI news"]
    fallback = MockProvider(["not json"])
    assert expand_search_queries("AI", "zh", fallback)[0] == "AI"


def test_scrape_partial_failure_and_cancel(monkeypatch):
    def fake_scrape(url, source_name, **kwargs):
        if url.endswith("/bad"):
            raise RuntimeError("failed")
        return article(1)

    monkeypatch.setattr(
        "app.pipeline.search_create.scrape_single", fake_scrape)
    options = SearchCreateOptions(topic="AI", ref_count=5, workers=2)
    rows = scrape_search_hits([
        SearchHit("good", "https://one.test/good"),
        SearchHit("bad", "https://two.test/bad"),
    ], options)
    assert len(rows) == 1

    with pytest.raises(SearchCreateCancelled):
        scrape_search_hits(
            [SearchHit("good", "https://one.test/good")],
            options, should_stop=lambda: True)


def test_rank_by_topic_uses_scores():
    rows = [article(1), article(2), article(3)]
    provider = MockProvider([
        '{"scores":[{"index":0,"score":2},{"index":1,"score":99},'
        '{"index":2,"score":40}]}'])
    assert [row.id for row in rank_by_topic(rows, "AI", provider, 2)] == [2, 3]


def test_synthesize_retries_links_citations_and_fact_checks():
    provider = MockProvider([
        "invalid",
        '{"title_candidates":["标题一","标题二"],'
        '"body_md":"## 进展\\n事实一 [1]，事实二 [2]。",'
        '"citations":[{"claim":"事实一","source_indexes":[1]}]}',
        '{"flagged_claims":["事实二缺少直接证据"]}',
    ])
    result = synthesize("AI", [article(1), article(2)], provider)
    assert result.title_candidates[0] == "标题一"
    assert "[[1]](https://example1.com/news)" in result.body_md
    assert "## 参考文献" in result.body_md
    assert result.citations[0]["source_indexes"] == [1]
    assert result.flagged_claims == ["事实二缺少直接证据"]


def test_synthesize_requires_multiple_sources():
    with pytest.raises(ValueError, match="至少需要 2"):
        synthesize("AI", [article(1)], MockProvider())


def test_run_search_create_mock_end_to_end(tmp_path, monkeypatch):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    conn = connect(config.db_path)
    init_db(conn)
    store = Store(conn, config)
    rows = [article(1), article(2)]
    monkeypatch.setattr(
        "app.pipeline.search_create.search_queries",
        lambda *args, **kwargs: [
            SearchHit("one", rows[0].url), SearchHit("two", rows[1].url)])
    monkeypatch.setattr(
        "app.pipeline.search_create.scrape_search_hits",
        lambda *args, **kwargs: rows)
    monkeypatch.setattr("app.store._web_image_for_title", lambda *args: None)
    provider = MockProvider([
        '{"queries":["AI research"]}',
        "[0,1]",
        '{"title_candidates":["综合标题"],'
        '"body_md":"## 正文\\n事实 [1] 和事实 [2]。",'
        '"citations":[{"claim":"事实","source_indexes":[1,2]}]}',
        '{"flagged_claims":[]}',
    ])
    events = []
    result = run_search_create(
        store, provider,
        SearchCreateOptions(topic="AI", time_range_days=0, ref_count=5),
        progress=events.append)
    assert result["draft_id"]
    assert events[-1]["stage"] == "done"
    meta = store.read_draft_body(result["draft_id"])
    assert meta["origin"] == "search_create"
    assert len(meta["sources"]) == 2
