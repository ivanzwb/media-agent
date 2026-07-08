from app.feeds import load_feeds, ensure_feeds_file, FeedsConfig

YAML = """
topics:
  - name: AI
    keywords: [AI, LLM]
  - name: 物理AI
    keywords: [robotics, world model]
sources:
  - name: OpenAI Blog
    type: rss
    url: https://openai.com/blog/rss.xml
    topics: [AI]
  - name: DeepMind Blog
    type: scrape
    mode: list
    url: https://deepmind.google/discover/blog/
    include_pattern: /discover/blog/
    topics: [AI, 物理AI]
"""


def test_load_feeds_parses_topics_and_sources(tmp_path):
    p = tmp_path / "feeds.yaml"
    p.write_text(YAML, encoding="utf-8")
    cfg = load_feeds(p)
    assert isinstance(cfg, FeedsConfig)
    assert {t.name for t in cfg.topics} == {"AI", "物理AI"}
    assert cfg.topics[0].keywords == ["AI", "LLM"]
    assert len(cfg.sources) == 2
    assert cfg.sources[1].mode == "list"
    assert cfg.sources[1].include_pattern == "/discover/blog/"


def test_load_feeds_defaults_mode_single_for_scrape(tmp_path):
    p = tmp_path / "feeds.yaml"
    p.write_text("""
topics: [{name: AI, keywords: [AI]}]
sources: [{name: S, type: scrape, url: https://x.com/a, topics: [AI]}]
""", encoding="utf-8")
    cfg = load_feeds(p)
    assert cfg.sources[0].mode == "single"


def test_load_feeds_missing_file_returns_empty(tmp_path):
    # Fresh/packaged install has no feeds.yaml — must not crash.
    cfg = load_feeds(tmp_path / "nope.yaml")
    assert isinstance(cfg, FeedsConfig)
    assert cfg.topics == [] and cfg.sources == []


def test_ensure_feeds_file_creates_empty_template_when_missing(tmp_path):
    p = tmp_path / "sub" / "feeds.yaml"
    assert not p.exists()
    ensure_feeds_file(p)
    assert p.exists()
    # The created file is a valid, empty template.
    cfg = load_feeds(p)
    assert cfg.topics == [] and cfg.sources == []


def test_ensure_feeds_file_does_not_overwrite_existing(tmp_path):
    p = tmp_path / "feeds.yaml"
    p.write_text(YAML, encoding="utf-8")
    ensure_feeds_file(p)
    # Existing content is preserved (only 2 sources from YAML, not defaults).
    cfg = load_feeds(p)
    assert len(cfg.sources) == 2
