from unittest.mock import patch, MagicMock

from app.llm.providers.mock import MockProvider
from app.pipeline.recommender import (
    suggest_subtopics, suggest_keywords, suggest_sources)
from app.feeds import SourceConfig


def _make_rss_source(name, url):
    return SourceConfig(name=name, type="rss", url=url)


def test_suggest_subtopics_uses_llm_consolidated_list():
    # The LLM is authoritative: its consolidated (de-overlapped) list is
    # returned as-is. Seeds are only passed as hints, not force-merged, so
    # overlapping subtopics no longer sneak back in from the seed list.
    provider = MockProvider(responses=['["脑机接口", "量子计算"]'])
    out = suggest_subtopics(["科技"], provider)
    assert out == ["脑机接口", "量子计算"]
    assert len(out) <= 24


def test_suggest_subtopics_extracts_embedded_array():
    provider = MockProvider(responses=['好的：["具身智能", "量子计算"] 仅供参考'])
    out = suggest_subtopics(["科技"], provider)
    assert "具身智能" in out
    assert "量子计算" in out


def test_suggest_subtopics_falls_back_when_unparseable():
    out = suggest_subtopics(["科技"], MockProvider())
    assert "人工智能" in out


def test_suggest_subtopics_generic_fallback_for_unknown_theme():
    out = suggest_subtopics(["园艺"], MockProvider())
    assert out
    assert any("园艺" in s for s in out)


def test_suggest_keywords_merges_seed_and_llm():
    provider = MockProvider(responses=['["LLM", "GPT"]'])
    out = suggest_keywords("人工智能", provider, limit=20)
    assert "AI" in out
    assert "LLM" in out
    assert "GPT" in out
    assert out.index("AI") < out.index("LLM")


def test_suggest_keywords_seed_fallback():
    out = suggest_keywords("脑机接口", MockProvider())
    assert "BCI" in out


def test_empty_input_returns_empty():
    assert suggest_subtopics([], MockProvider()) == []
    assert suggest_keywords("  ", MockProvider()) == []


def test_suggest_sources_discover_from_url():
    """When LLM gives a URL, suggest_sources should discover RSS from it."""
    provider = MockProvider(responses=[
        '[{"name": "OpenAI", "url": "https://openai.com/blog"}]'
    ])

    def mock_discover(url):
        return [_make_rss_source("OpenAI Blog", "https://openai.com/blog/rss")]

    with patch("app.discovery.discover_from_url", side_effect=mock_discover):
        with patch("app.discovery.discover_from_keyword"):
            out = suggest_sources("AI", provider)

    urls = [o["url"] for o in out]
    assert "https://openai.com/blog/rss" in urls
    assert out[0]["name"] == "OpenAI"


def test_suggest_sources_discover_by_name():
    """When LLM URL fails discovery, fall back to search by name."""
    provider = MockProvider(responses=[
        '[{"name": "Anthropic", "url": "https://anthropic.com"}]'
    ])

    def mock_discover(url):
        return []

    def mock_keyword(query, max_sites=2):
        return [_make_rss_source("Anthropic Blog", "https://anthropic.com/feed")]

    with patch("app.discovery.discover_from_url", side_effect=mock_discover):
        with patch("app.discovery.discover_from_keyword", side_effect=mock_keyword):
            out = suggest_sources("AI", provider)

    urls = [o["url"] for o in out]
    assert "https://anthropic.com/feed" in urls


def test_suggest_sources_seed_fallback():
    """When LLM returns nothing, fall back to seed data with discovery."""
    provider = MockProvider()

    def mock_discover(url):
        return []

    def mock_keyword(query, max_sites=2):
        return []

    with patch("app.discovery.discover_from_url", side_effect=mock_discover):
        with patch("app.discovery.discover_from_keyword", side_effect=mock_keyword):
            out = suggest_sources("AI", provider)

    names = [o["name"] for o in out]
    assert "OpenAI" in names
    assert all(o["url"].startswith("http") for o in out)


def test_suggest_sources_dedupes():
    """Same URL from different sources should be deduped."""
    provider = MockProvider(responses=[
        '[{"name": "Company A", "url": "https://a.com"},'
        ' {"name": "Company B", "url": "https://a.com/blog"}]'
    ])

    def mock_discover(url):
        if "a.com" in url:
            return [_make_rss_source("A Blog", "https://a.com/feed")]
        return []

    with patch("app.discovery.discover_from_url", side_effect=mock_discover):
        with patch("app.discovery.discover_from_keyword"):
            out = suggest_sources("AI", provider, limit=10)

    urls = [o["url"] for o in out]
    assert len(set(urls)) == len(urls)


def test_suggest_sources_limit():
    """Should respect the limit parameter."""
    provider = MockProvider(responses=[
        '[{"name": "A", "url": "https://a.com"},'
        ' {"name": "B", "url": "https://b.com"},'
        ' {"name": "C", "url": "https://c.com"}]'
    ])

    def mock_discover(url):
        return []

    with patch("app.discovery.discover_from_url", side_effect=mock_discover):
        with patch("app.discovery.discover_from_keyword"):
            out = suggest_sources("AI", provider, limit=2)

    assert len(out) <= 2


def test_suggest_sources_empty_topic():
    assert suggest_sources("  ", MockProvider()) == []


def test_suggest_sources_no_urls_when_all_discovery_fails():
    """When LLM gives names but all discovery fails and no seed, returns empty."""
    provider = MockProvider(responses=[
        '[{"name": "UnknownCorp", "url": "https://unknown.com"}]'
    ])

    def mock_discover(url):
        return []

    def mock_keyword(query, max_sites=2):
        return []

    with patch("app.discovery.discover_from_url", side_effect=mock_discover):
        with patch("app.discovery.discover_from_keyword", side_effect=mock_keyword):
            out = suggest_sources("niche_topic_xyz", provider)

    assert out == []
