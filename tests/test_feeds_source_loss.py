"""Reproduce: re-recommend subtopics + discover sources → old sources lost."""

from fastapi.testclient import TestClient

from app.config import Config
from app.db import connect, init_db
from app.store import Store
from app.web.server import create_app
from app.feeds import load_feeds, save_feeds, FeedsConfig, Topic, SourceConfig


def _make_client(tmp_path):
    cfg = Config(data_dir=tmp_path, llm_provider="mock", image_provider="mock")
    cfg.ensure_dirs()
    conn = connect(cfg.db_path)
    init_db(conn)
    store = Store(conn, cfg)

    feeds = tmp_path / "feeds.yaml"
    # Seed with 2 existing topics + 3 existing sources
    existing = FeedsConfig(
        topics=[Topic(name="AI", keywords=["LLM", "GPT"]),
                Topic(name="机器人", keywords=["robotics", "embodied"])],
        sources=[
            SourceConfig(name="OpenAI Blog", type="rss",
                         url="https://openai.com/blog/rss.xml",
                         topics=["AI"]),
            SourceConfig(name="DeepMind Blog", type="scrape",
                         url="https://deepmind.google/discover/blog/",
                         topics=["AI", "机器人"], mode="list",
                         include_pattern="/discover/blog/"),
            SourceConfig(name="Meta AI Blog", type="scrape",
                         url="https://ai.meta.com/blog/",
                         topics=["AI"], mode="list"),
        ])
    save_feeds(existing, feeds)

    app = create_app(cfg, feeds_path=feeds)
    client = TestClient(app)
    return client, store, feeds


def test_add_topic_preserves_existing_sources(tmp_path):
    """POST /sources/topics/add must NOT delete existing sources."""
    client, store, feeds = _make_client(tmp_path)

    # Add a new topic (simulates user clicking "加为主题")
    r = client.post("/sources/topics/add",
                    data={"name": "AI芯片", "keywords": "NVIDIA, GPU, NPU"},
                    follow_redirects=False)
    assert r.status_code == 303

    cfg = load_feeds(feeds)
    # 3 original topics + 1 new = 4
    assert len(cfg.topics) == 3, f"Expected 3 topics, got {len(cfg.topics)}"
    # ALL 3 original sources MUST survive
    assert len(cfg.sources) == 3, f"Expected 3 sources, got {len(cfg.sources)}"
    urls = {s.url for s in cfg.sources}
    assert "https://openai.com/blog/rss.xml" in urls
    assert "https://deepmind.google/discover/blog/" in urls
    assert "https://ai.meta.com/blog/" in urls


def test_discover_add_preserves_existing_sources(tmp_path, monkeypatch):
    """POST /sources/discover-add must NOT delete existing sources."""
    from app.feeds import SourceConfig

    client, store, feeds = _make_client(tmp_path)

    # Monkeypatch discover_from_url to return a new source
    monkeypatch.setattr(
        "app.web.server.discover_from_url",
        lambda url, topics=None: [SourceConfig(
            name="New Source", type="rss",
            url="https://newsource.com/feed.xml",
            topics=topics or [])])

    # Simulate user clicking "添加选中来源" for topic "AI"
    r = client.post("/sources/discover-add",
                    data={"url": "https://newsource.com", "topics": "AI"})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] in ("added", "exists")

    cfg = load_feeds(feeds)
    # 3 original + 1 new = 4
    assert len(cfg.sources) == 4, \
        f"Expected 4 sources, got {len(cfg.sources)}: {[s.url for s in cfg.sources]}"
    urls = {s.url for s in cfg.sources}
    assert "https://openai.com/blog/rss.xml" in urls, "Original source 1 lost!"
    assert "https://deepmind.google/discover/blog/" in urls, "Original source 2 lost!"
    assert "https://ai.meta.com/blog/" in urls, "Original source 3 lost!"
    assert "https://newsource.com/feed.xml" in urls, "New source not added"


def test_full_flow_preserves_all_data(tmp_path, monkeypatch):
    """Simulate complete user flow: add topic + discover sources.

    Flow:
      1. Start with existing topics + sources
      2. POST /sources/topics/add → new topic
      3. POST /sources/discover-add → new source for the new topic
      4. Verify ALL original sources + topics survive
    """
    from app.feeds import SourceConfig

    client, store, feeds = _make_client(tmp_path)

    # Step 1: Add a new topic via recommendation
    r = client.post("/sources/topics/add",
                    data={"name": "量子计算", "keywords": "quantum, QPU"},
                    follow_redirects=False)
    assert r.status_code == 303

    # Step 2: Discover sources for the new topic
    monkeypatch.setattr(
        "app.web.server.discover_from_url",
        lambda url, topics=None: [SourceConfig(
            name="IBM Quantum", type="rss",
            url="https://ibm.com/quantum/feed.xml",
            topics=topics or [])])

    r2 = client.post("/sources/discover-add",
                     data={"url": "https://ibm.com/quantum", "topics": "量子计算"})
    assert r2.status_code == 200

    # Step 3: Verify ALL data survives
    cfg = load_feeds(feeds)

    # Topics: AI, 机器人 (original) + 量子计算 (new) = 3
    topic_names = {t.name for t in cfg.topics}
    assert topic_names == {"AI", "机器人", "量子计算"}, \
        f"Unexpected topics: {topic_names}"

    # Sources: 3 original + 1 new = 4
    assert len(cfg.sources) == 4, \
        f"Expected 4 sources, got {len(cfg.sources)}"
    urls = {s.url for s in cfg.sources}
    assert "https://openai.com/blog/rss.xml" in urls
    assert "https://deepmind.google/discover/blog/" in urls
    assert "https://ai.meta.com/blog/" in urls
    assert "https://ibm.com/quantum/feed.xml" in urls

    # Step 4: Verify source-topics associations preserved
    for s in cfg.sources:
        if s.url == "https://deepmind.google/discover/blog/":
            assert "AI" in s.topics and "机器人" in s.topics
        if s.url == "https://ibm.com/quantum/feed.xml":
            assert "量子计算" in s.topics


def test_multiple_discover_adds_preserve_all(tmp_path, monkeypatch):
    """Adding sources sequentially via discover-add must not lose data."""
    from app.feeds import SourceConfig
    import time

    client, store, feeds = _make_client(tmp_path)

    discover_count = [0]

    def fake_discover(url, topics=None):
        discover_count[0] += 1
        return [SourceConfig(
            name=f"Source-{discover_count[0]}", type="rss",
            url=f"https://src{discover_count[0]}.com/feed",
            topics=topics or [])]

    monkeypatch.setattr("app.web.server.discover_from_url", fake_discover)

    # Simulate adding 5 sources sequentially (like topicAddSources does)
    for i in range(5):
        r = client.post("/sources/discover-add",
                        data={"url": f"https://src{i}.com", "topics": "AI"})
        assert r.status_code == 200

    cfg = load_feeds(feeds)
    # 3 original + 5 new = 8
    assert len(cfg.sources) == 8, \
        f"Expected 8 sources, got {len(cfg.sources)}"
    urls = {s.url for s in cfg.sources}
    for i in range(5):
        assert f"https://src{i+1}.com/feed" in urls, f"New source {i+1} missing!"
