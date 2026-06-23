from app.feeds import load_feeds, save_feeds, FeedsConfig, Topic, SourceConfig


def test_save_feeds_roundtrip(tmp_path):
    cfg = FeedsConfig(
        topics=[Topic(name="AI", keywords=["LLM", "GPT"]),
                Topic(name="物理AI", keywords=["robotics"])],
        sources=[
            SourceConfig(name="OpenAI", type="rss",
                         url="https://openai.com/rss.xml", topics=["AI"]),
            SourceConfig(name="DeepMind", type="scrape",
                         url="https://deepmind.google/blog/", topics=["AI"],
                         mode="list", include_pattern="/blog/"),
        ])
    path = tmp_path / "feeds.yaml"
    save_feeds(cfg, path)
    loaded = load_feeds(path)
    assert {t.name for t in loaded.topics} == {"AI", "物理AI"}
    assert loaded.sources[0].type == "rss"
    assert loaded.sources[1].mode == "list"
    assert loaded.sources[1].include_pattern == "/blog/"
