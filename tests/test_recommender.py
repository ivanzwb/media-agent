from app.llm.providers.mock import MockProvider
from app.pipeline.recommender import (
    suggest_subtopics, suggest_keywords, suggest_sources)


def test_suggest_subtopics_parses_json_array():
    provider = MockProvider(responses=['["人工智能", "脑机接口", "量子计算"]'])
    out = suggest_subtopics(["科技"], provider)
    assert out == ["人工智能", "脑机接口", "量子计算"]


def test_suggest_subtopics_extracts_embedded_array():
    provider = MockProvider(responses=['好的：["AI", "机器人"] 仅供参考'])
    out = suggest_subtopics(["科技"], provider)
    assert out == ["AI", "机器人"]


def test_suggest_subtopics_falls_back_when_unparseable():
    # Default MockProvider echoes "[mock] ..." which is not a JSON array.
    out = suggest_subtopics(["科技"], MockProvider())
    assert "人工智能" in out  # from built-in seed for 科技


def test_suggest_subtopics_generic_fallback_for_unknown_theme():
    out = suggest_subtopics(["园艺"], MockProvider())
    assert out  # non-empty generic suggestions
    assert any("园艺" in s for s in out)


def test_suggest_keywords_parses_and_dedupes():
    provider = MockProvider(responses=['["AI", "AI", "LLM", "GPT"]'])
    out = suggest_keywords("人工智能", provider, limit=10)
    assert out == ["AI", "LLM", "GPT"]


def test_suggest_keywords_seed_fallback():
    out = suggest_keywords("脑机接口", MockProvider())
    assert "BCI" in out


def test_empty_input_returns_empty():
    assert suggest_subtopics([], MockProvider()) == []
    assert suggest_keywords("  ", MockProvider()) == []


def test_suggest_sources_parses_objects_and_dedupes():
    provider = MockProvider(responses=[
        '[{"name": "OpenAI", "url": "https://openai.com/news/"},'
        ' {"name": "Dup", "url": "https://openai.com/news"},'
        ' {"name": "Anthropic", "url": "https://www.anthropic.com/news"}]'
    ])
    out = suggest_sources("AI", provider)
    urls = [o["url"] for o in out]
    assert "https://openai.com/news/" in urls
    # second item normalizes to same url (trailing slash) -> deduped
    assert len(urls) == 2
    assert out[0]["name"] == "OpenAI"


def test_suggest_sources_seed_fallback():
    out = suggest_sources("AI", MockProvider())
    names = [o["name"] for o in out]
    assert "OpenAI" in names
    assert all(o["url"].startswith("http") for o in out)


def test_suggest_sources_ignores_items_without_url():
    provider = MockProvider(responses=['[{"name": "NoUrl"}, {"foo": "bar"}]'])
    assert suggest_sources("AI", provider) == suggest_sources(
        "AI", MockProvider())  # falls back to seed when nothing usable


def test_suggest_sources_empty_topic():
    assert suggest_sources("  ", MockProvider()) == []
