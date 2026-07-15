import json

from app.llm.providers.mock import MockProvider
from app.pipeline.recommender import (
    suggest_subtopics, suggest_keywords, suggest_sources,
    suggest_keywords_batch, suggest_source_names_batch, _resolve_sources,
    _KEYWORD_SEED)


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


def test_suggest_sources_homepage_url_is_scrape():
    """A candidate with a normal homepage URL is kept as type="scrape".

    Resolution is network-free: no RSS autodiscovery, feed probing, or web
    search happens (registering the correct URL is enough — the scraper decides
    what to crawl)."""
    provider = MockProvider(responses=[
        '[{"name": "OpenAI", "url": "https://openai.com/blog"}]'
    ])

    out = suggest_sources("AI", provider)

    assert out == [{"name": "OpenAI", "url": "https://openai.com/blog",
                    "type": "scrape"}]


def test_suggest_sources_feed_url_is_rss():
    """A candidate whose URL LOOKS like a feed (cheap heuristic, no network) is
    tagged type="rss"."""
    provider = MockProvider(responses=[
        '[{"name": "SomeBlog", "url": "https://x.com/rss.xml"}]'
    ])

    out = suggest_sources("AI", provider)

    assert out == [{"name": "SomeBlog", "url": "https://x.com/rss.xml",
                    "type": "rss"}]


def test_suggest_sources_skips_candidate_without_url():
    """A candidate with no/invalid URL is skipped (a source needs a URL).

    Here the LLM candidate has no usable URL, so Phase A yields nothing and the
    seed fallback for a seeded topic kicks in."""
    provider = MockProvider(responses=[
        '[{"name": "NoUrlCorp", "url": ""},'
        ' {"name": "BadCorp", "url": "not-a-url"}]'
    ])

    out = suggest_sources("人工智能", provider)

    # Neither invalid candidate is kept; seed fallback (seeded topic) fills in.
    assert all(o["url"].startswith("http") for o in out)
    assert "NoUrlCorp" not in [o["name"] for o in out]
    assert "OpenAI" in [o["name"] for o in out]


def test_suggest_sources_seed_fallback():
    """When the LLM returns nothing, fall back to curated seed sources,
    classified the same cheap (network-free) way."""
    out = suggest_sources("AI", MockProvider())

    names = [o["name"] for o in out]
    assert "OpenAI" in names
    assert all(o["url"].startswith("http") for o in out)
    # Seed URLs are homepages → scrape.
    assert all(o["type"] == "scrape" for o in out)


def test_suggest_sources_dedupes():
    """Candidates that normalize to the same URL should be deduped."""
    provider = MockProvider(responses=[
        '[{"name": "Company A", "url": "https://a.com/blog"},'
        ' {"name": "Company B", "url": "https://a.com/blog/"}]'
    ])

    out = suggest_sources("AI", provider, limit=10)

    urls = [o["url"] for o in out]
    assert len(set(u.rstrip("/") for u in urls)) == len(urls)
    assert len(out) == 1


def test_suggest_sources_limit():
    """Should respect the limit parameter."""
    provider = MockProvider(responses=[
        '[{"name": "A", "url": "https://a.com"},'
        ' {"name": "B", "url": "https://b.com"},'
        ' {"name": "C", "url": "https://c.com"}]'
    ])

    out = suggest_sources("AI", provider, limit=2)

    assert len(out) <= 2


def test_suggest_sources_empty_topic():
    assert suggest_sources("  ", MockProvider()) == []


def test_suggest_sources_keeps_scrape_when_no_rss():
    """A candidate with a usable homepage URL (no feed markers) is kept as a
    scrape source (discovery must not drop non-RSS sites)."""
    provider = MockProvider(responses=[
        '[{"name": "UnknownCorp", "url": "https://unknown.com"}]'
    ])

    out = suggest_sources("niche_topic_xyz", provider)

    assert out == [{"name": "UnknownCorp", "url": "https://unknown.com",
                    "type": "scrape"}]


def test_resolve_sources_all_scrape_when_none_look_like_feeds():
    """Candidates with plain homepage/section URLs are all kept as scrape
    sources (RSS type is only a cheap URL-shape upgrade, never a gate)."""
    candidates = [
        {"name": "A", "url": "https://a.com"},
        {"name": "B", "url": "https://b.com/news"},
        {"name": "C", "url": "https://c.com/blog"},
    ]

    out = _resolve_sources("topic", candidates)

    assert [o["url"] for o in out] == [c["url"] for c in candidates]
    assert all(o["type"] == "scrape" for o in out)


def test_resolve_sources_classifies_feed_urls_as_rss():
    """URL-shape heuristic: feed-looking URLs → rss, others → scrape."""
    candidates = [
        {"name": "Home", "url": "https://a.com"},
        {"name": "Feed1", "url": "https://a.com/feed"},
        {"name": "Feed2", "url": "https://b.com/atom.xml"},
        {"name": "Feed3", "url": "https://c.com/blog?feed=rss2"},
        {"name": "Section", "url": "https://d.com/news"},
    ]

    out = _resolve_sources("topic", candidates)

    by_name = {o["name"]: o["type"] for o in out}
    assert by_name == {
        "Home": "scrape", "Feed1": "rss", "Feed2": "rss",
        "Feed3": "rss", "Section": "scrape",
    }


def test_resolve_sources_skips_candidates_without_url():
    """Candidates lacking a usable http(s) URL are skipped entirely."""
    candidates = [
        {"name": "Good", "url": "https://good.com"},
        {"name": "Empty", "url": ""},
        {"name": "Bad", "url": "ftp://nope.com"},
        {"name": "Missing"},
    ]

    out = _resolve_sources("topic", candidates)

    assert out == [{"name": "Good", "url": "https://good.com",
                    "type": "scrape"}]


def test_batched_source_resolution_matches_manual():
    """One-click batched resolution must equal manual per-topic suggest_sources
    for the same candidates (best-effort seed URLs included)."""
    topics = ["人工智能", "视频生成"]  # one seeded, one not
    batch_json = json.dumps({
        "人工智能": [{"name": "OpenAI", "url": "https://openai.com"}],
        "视频生成": [{"name": "Runway", "url": "https://runwayml.com"}],
    }, ensure_ascii=False)
    name_map = suggest_source_names_batch(
        topics, MockProvider(responses=[batch_json]))

    for t in topics:
        candidates = name_map[t]
        arr = json.dumps(candidates, ensure_ascii=False)
        manual = suggest_sources(t, MockProvider(responses=[arr]))
        batched = _resolve_sources(t, candidates)
        assert manual == batched
        # Homepage URLs → scrape (no network, cheap URL heuristic).
        assert all(o["type"] == "scrape" for o in batched)
    # Both topics keep their candidate URLs as best-effort scrape sources —
    # both mirror the manual path exactly.
    assert any(o["name"] == "OpenAI" for o in name_map["人工智能"])


def test_suggest_source_names_batch_chunks_cover_all_topics():
    """More topics than one chunk → multiple agent calls (consumed in order by
    MockProvider) and every topic gets its chunk's candidates."""
    from app.pipeline.recommender import _SOURCE_BATCH_CHUNK
    # 10 topics with a chunk size of 7 → 2 chunks (7 + 3).
    topics = [f"主题{i}" for i in range(10)]
    assert len(topics) > _SOURCE_BATCH_CHUNK  # ensure we exercise >1 chunk

    def _chunk_json(chunk_topics):
        return json.dumps(
            {t: [{"name": f"Org-{t}", "url": f"https://{t}.example.com"}]
             for t in chunk_topics}, ensure_ascii=False)

    chunk1 = _chunk_json(topics[:_SOURCE_BATCH_CHUNK])
    chunk2 = _chunk_json(topics[_SOURCE_BATCH_CHUNK:])
    provider = MockProvider(responses=[chunk1, chunk2])

    out = suggest_source_names_batch(topics, provider)

    assert set(out) == set(topics)
    for t in topics:
        assert out[t], f"{t} had no candidates"
        assert all(c["url"].startswith("http") for c in out[t])


def test_suggest_source_names_batch_bad_chunk_falls_back_to_seeds():
    """A chunk whose call parses empty/garbage falls back to curated seeds for
    ITS topics without losing the other chunks' results."""
    from app.pipeline.recommender import _SOURCE_BATCH_CHUNK, _SOURCE_SEED
    # Chunk 1 (indices 0..6): arbitrary topics the agent answers.
    good_topics = [f"主题{i}" for i in range(_SOURCE_BATCH_CHUNK)]
    # Chunk 2: all curated-seed topics, so seed fallback yields candidates when
    # the second agent call returns garbage.
    seeded_topics = ["人工智能", "机器人", "脑机接口"]
    assert all(t.lower() in _SOURCE_SEED for t in seeded_topics)
    topics = good_topics + seeded_topics

    good_json = json.dumps(
        {t: [{"name": f"Org-{t}", "url": f"https://{t}.example.com"}]
         for t in good_topics}, ensure_ascii=False)
    # Second chunk response is unparseable garbage → forces seed fallback.
    provider = MockProvider(responses=[good_json, "totally not json ###"])

    out = suggest_source_names_batch(topics, provider)

    # Chunk 1 results preserved.
    for t in good_topics:
        assert out[t], f"{t} lost its agent candidates"
        assert out[t][0]["url"].startswith("http")
    # Chunk 2 garbage → curated seed fallback per topic.
    for t in seeded_topics:
        assert out[t], f"{t} seed fallback was empty"
        assert all(c["url"].startswith("http") for c in out[t])
        seed_names = {it["name"] for it in _SOURCE_SEED[t.lower()]}
        assert {c["name"] for c in out[t]} == seed_names


def test_batched_keywords_superset_of_seed_and_matches_manual():
    """Batched keywords must never be emptier than the manual per-subtopic
    seed floor, and must equal the manual path when the LLM is unavailable."""
    subs = ["人工智能", "脑机接口"]
    batch = suggest_keywords_batch(subs, MockProvider())  # offline → seeds only
    for s in subs:
        seed = set(_KEYWORD_SEED.get(s.lower(), []))
        assert seed.issubset(set(batch[s]))
        manual = suggest_keywords(s, MockProvider())
        assert batch[s] == manual
