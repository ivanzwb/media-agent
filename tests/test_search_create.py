from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sys
import types

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
    SearchHit, merge_search_hits, normalize_search_url, search_bing_text,
    search_ddgs_text, search_query)
from app.sources.dedup import dedup_near_content
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
    monkeypatch.setattr("app.sources.web_search.time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "app.sources.web_search.search_ddgs_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(
        "app.sources.web_search.search_web",
        lambda *args, **kwargs: [
            "https://fallback.test/a?utm_campaign=x",
            "https://fallback.test/a",
        ])
    monkeypatch.setattr(
        "app.sources.web_search.search_bing_text",
        lambda *args, **kwargs: [])
    hits = search_query("topic", engines=["duckduckgo"])
    assert len(hits) == 2  # fallback preserves discovery output; merge removes it
    assert merge_search_hits([hits]) == [
        SearchHit("https://fallback.test/a?utm_campaign=x",
                  "https://fallback.test/a", "")]


def test_ddgs_uses_selected_search_engines(monkeypatch):
    captured = {}

    class FakeDDGS:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        def text(self, query, **kwargs):
            captured.update(kwargs)
            return [{"title": "A", "href": "https://example.test/a"}]

    monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=FakeDDGS))
    hits = search_ddgs_text(
        "topic", engines=["duckduckgo", "google"], max_results=5,
        proxy="http://127.0.0.1:7890", timeout=7)
    assert hits[0].url == "https://example.test/a"
    assert captured["backend"] == "duckduckgo,google"
    assert captured["client"] == {
        "proxy": "http://127.0.0.1:7890", "timeout": 7}


def test_bing_result_short_circuits_blocked_engines(monkeypatch):
    expected = SearchHit("Bing result", "https://example.test/bing")
    monkeypatch.setattr(
        "app.sources.web_search.search_bing_text",
        lambda *args, **kwargs: [expected])
    monkeypatch.setattr(
        "app.sources.web_search.search_ddgs_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("DDGS should not run after Bing succeeds")))
    assert search_query(
        "topic", region="cn-zh", engines=["duckduckgo"]) == [expected]


def test_search_retries_three_times_before_success(monkeypatch):
    calls = {"count": 0}
    delays = []
    expected = SearchHit("Recovered", "https://example.test/recovered")

    def flaky_bing(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] <= 3:
            raise TimeoutError("temporary timeout")
        return [expected]

    monkeypatch.setattr(
        "app.sources.web_search.search_bing_text", flaky_bing)
    monkeypatch.setattr(
        "app.sources.web_search.time.sleep", delays.append)
    assert search_query(
        "topic", region="cn-zh", engines=["duckduckgo"]) == [expected]
    assert calls["count"] == 4  # initial attempt + three retries
    assert delays == [0.4, 0.8, 1.6]


def test_bing_html_search_parses_results(monkeypatch):
    page = """
    <ol>
      <li class="b_algo"><h2><a href="https://news.test/a?utm_source=bing">
      Result A</a></h2><div><p>Useful summary.</p></div></li>
    </ol>
    """

    class Response:
        text = page

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url, params):
            assert url == "https://cn.bing.com/search"
            assert params["setlang"] == "zh-hans"
            return Response()

    monkeypatch.setattr("app.sources.web_search.httpx.Client", Client)
    hits = search_bing_text("topic", region="cn-zh")
    assert hits == [
        SearchHit("Result A", "https://news.test/a", "Useful summary.")]


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


def test_rank_by_topic_falls_back_to_lexical_and_diversifies_sources():
    rows = [article(1), article(2), article(3)]
    rows[0].title = "Unrelated"
    rows[0].source_name = "same.example"
    rows[1].title = "Quantum computing breakthrough"
    rows[1].source_name = "same.example"
    rows[2].title = "Quantum computing industry analysis"
    rows[2].source_name = "other.example"
    ranked = rank_by_topic(
        rows, "quantum computing", MockProvider(["invalid"]), 2)
    assert [row.id for row in ranked] == [2, 3]


def test_near_content_dedup_keeps_richer_copy():
    first = article(1)
    second = article(2)
    first.title = second.title = "Same report"
    first.content_md = "shared research result " * 80
    second.content_md = first.content_md + "additional detail " * 10
    unique = article(3)
    unique.content_md = "completely different subject " * 30
    result = dedup_near_content([first, second, unique])
    assert [row.id for row in result] == [2, 3]


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


def test_synthesize_applies_requested_output_language():
    class CaptureProvider(MockProvider):
        def __init__(self):
            super().__init__([
                '{"title_candidates":["Title"],"body_md":"## Body\\nFact [1]."}',
                '{"flagged_claims":[]}',
            ])
            self.calls = []

        def chat(self, messages, **opts):
            self.calls.append(messages)
            return super().chat(messages, **opts)

    provider = CaptureProvider()
    synthesize("AI", [article(1), article(2)], provider, lang="en")
    assert "输出语言：English" in provider.calls[0][-1].content


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
        '{"scores":[{"index":0,"score":90},{"index":1,"score":80}]}',
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
