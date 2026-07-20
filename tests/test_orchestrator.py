import json
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import ANY, patch

import httpx

from app.config import Config
from app.db import connect, init_db
from app.store import Store
from app.models import Article
from app.feeds import FeedsConfig, Topic, SourceConfig
from app.llm.providers.mock import MockProvider
from app.pipeline.orchestrator import run_pipeline, filter_by_age


def _article(published_at):
    return Article(title="t", content_md="body", url=f"https://x.com/{id(published_at)}",
                   source_name="X", source_type="rss", published_at=published_at,
                   images=[], raw_summary=None,
                   fetched_at=datetime.now(timezone.utc))


def test_filter_by_age_drops_undated_when_max_age_set():
    now = datetime.now(timezone.utc)
    recent = _article(now - timedelta(days=1))
    old = _article(now - timedelta(days=40))
    undated = _article(None)
    kept = filter_by_age([recent, old, undated], 7, now=now)
    assert kept == [recent]


def test_filter_by_age_keeps_undated_when_max_age_none():
    now = datetime.now(timezone.utc)
    recent = _article(now - timedelta(days=1))
    undated = _article(None)
    kept = filter_by_age([recent, undated], None, now=now)
    assert kept == [recent, undated]


def test_filter_by_age_drops_old_keeps_recent():
    now = datetime.now(timezone.utc)
    recent = _article(now - timedelta(days=2))
    old = _article(now - timedelta(days=10))
    kept = filter_by_age([recent, old], 7, now=now)
    assert kept == [recent]


def build(tmp_path):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    conn = connect(cfg.db_path)
    init_db(conn)
    return cfg, Store(conn, cfg)


def fake_articles():
    return [Article(title="GPT-5 released", content_md="It scores high.",
                    url="https://x.com/a", source_name="X", source_type="rss",
                    published_at=None, images=[], raw_summary=None,
                    fetched_at=datetime.now(timezone.utc))]


def test_run_pipeline_end_to_end(tmp_path, monkeypatch):
    cfg, store = build(tmp_path)
    feeds = FeedsConfig(
        topics=[Topic(name="AI", keywords=["GPT"])],
        sources=[SourceConfig(name="X", type="rss", url="https://x.com/feed",
                              topics=["AI"])])

    monkeypatch.setattr("app.pipeline.orchestrator.collect_sources",
                        lambda feeds_cfg, max_per_source=None, progress=None, workers=None, proxy=None: fake_articles())

    rewrite_json = json.dumps({"title_candidates": ["爆款标题"],
                               "body_md": "## 钩子\n正文"})
    check_json = json.dumps({"flagged_claims": []})
    provider = MockProvider(responses=[rewrite_json, check_json])

    stats = run_pipeline(feeds, store, provider, max_drafts=5)
    assert stats["fetched"] == 1
    assert stats["archived"] == 1
    assert stats["drafted"] == 1

    drafts = store.conn.execute("SELECT * FROM drafts").fetchall()
    assert len(drafts) == 1
    articles = store.conn.execute("SELECT * FROM articles").fetchall()
    assert articles[0]["topic"] == "AI"


def test_run_pipeline_skips_duplicates(tmp_path, monkeypatch):
    cfg, store = build(tmp_path)
    feeds = FeedsConfig(topics=[Topic(name="AI", keywords=["GPT"])], sources=[])
    monkeypatch.setattr("app.pipeline.orchestrator.collect_sources",
                        lambda feeds_cfg, max_per_source=None, progress=None, workers=None, proxy=None: fake_articles())
    provider = MockProvider(responses=[
        json.dumps({"title_candidates": ["t"], "body_md": "b"}),
        json.dumps({"flagged_claims": []})])
    run_pipeline(feeds, store, provider, max_drafts=5)
    stats2 = run_pipeline(feeds, store, provider, max_drafts=5)
    assert stats2["archived"] == 0


def test_run_pipeline_skips_same_title_source(tmp_path, monkeypatch):
    """Articles with same title+source but different URLs (different fingerprints)
    are still deduped by the secondary exists_by_title_source() check."""
    cfg, store = build(tmp_path)

    # First run: saves article with URL-A
    monkeypatch.setattr("app.pipeline.orchestrator.collect_sources",
                        lambda feeds_cfg, max_per_source=None, progress=None, workers=None, proxy=None: [
        Article(title="Same Title", content_md="body", url="https://x.com/url-a",
                source_name="Test Source", source_type="rss", published_at=None,
                images=[], raw_summary=None,
                fetched_at=datetime.now(timezone.utc))])
    feeds = FeedsConfig(topics=[Topic(name="AI", keywords=["title"])], sources=[])
    provider = MockProvider(responses=[
        json.dumps({"title_candidates": ["t1"], "body_md": "b1"}),
        json.dumps({"flagged_claims": []})])
    stats1 = run_pipeline(feeds, store, provider, max_drafts=5)
    assert stats1["archived"] == 1

    # Second run: same title+source, different URL → should be skipped
    monkeypatch.setattr("app.pipeline.orchestrator.collect_sources",
                        lambda feeds_cfg, max_per_source=None, progress=None, workers=None, proxy=None: [
        Article(title="Same Title", content_md="body", url="https://x.com/url-b",
                source_name="Test Source", source_type="rss", published_at=None,
                images=[], raw_summary=None,
                fetched_at=datetime.now(timezone.utc))])
    provider2 = MockProvider(responses=[
        json.dumps({"title_candidates": ["t2"], "body_md": "b2"}),
        json.dumps({"flagged_claims": []})])
    stats2 = run_pipeline(feeds, store, provider2, max_drafts=5)
    assert stats2["archived"] == 0
    # Only 1 article in DB
    count = store.conn.execute("SELECT COUNT(*) AS c FROM articles").fetchone()["c"]
    assert count == 1


# ── failure logging ───────────────────────────────────────────────────────


def test_collect_sources_logs_http_failure(tmp_path, monkeypatch):
    """When _fetch_source raises HTTPStatusError, log_fetch_failure captures it."""
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    from app.pipeline.orchestrator import collect_sources

    feeds = FeedsConfig(
        topics=[],
        sources=[
            SourceConfig(name="BadFeed", type="rss",
                         url="https://bad.example.com/feed", topics=["AI"]),
        ],
    )

    # Mock _fetch_source to raise an HTTP 403
    request = httpx.Request("GET", "https://bad.example.com/feed")
    response = httpx.Response(403, request=request, text="blocked")
    exc = httpx.HTTPStatusError("Forbidden", request=request, response=response)

    with patch("app.pipeline.orchestrator._fetch_source", side_effect=exc):
        articles = collect_sources(feeds)
        assert articles == []  # failure is swallowed, no articles returned

    # Check the failure log
    logpath = tmp_path / "fetch_failures.log"
    assert logpath.exists()
    entry = json.loads(logpath.read_text(encoding="utf-8"))
    assert entry["source_name"] == "BadFeed"
    assert entry["error_type"] == "HTTPStatusError"
    assert entry["status_code"] == 403


def test_collect_sources_logs_timeout_failure(tmp_path, monkeypatch):
    """When _fetch_source raises TimeoutException, log_fetch_failure captures it."""
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    from app.pipeline.orchestrator import collect_sources

    feeds = FeedsConfig(
        topics=[],
        sources=[
            SourceConfig(name="SlowFeed", type="rss",
                         url="https://slow.example.com/feed", topics=["AI"]),
        ],
    )

    exc = httpx.TimeoutException("Connect timeout")
    with patch("app.pipeline.orchestrator._fetch_source", side_effect=exc):
        articles = collect_sources(feeds)
        assert articles == []

    entry = json.loads(
        (tmp_path / "fetch_failures.log").read_text(encoding="utf-8")
    )
    assert entry["source_name"] == "SlowFeed"
    assert entry["error_type"] == "TimeoutException"


def test_collect_sources_logs_connect_error(tmp_path, monkeypatch):
    """When _fetch_source raises ConnectError, log_fetch_failure captures it."""
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    from app.pipeline.orchestrator import collect_sources

    feeds = FeedsConfig(
        topics=[],
        sources=[
            SourceConfig(name="Unreachable", type="rss",
                         url="https://dead.example.com/feed", topics=["AI"]),
        ],
    )

    exc = httpx.ConnectError("Connection refused")
    with patch("app.pipeline.orchestrator._fetch_source", side_effect=exc):
        articles = collect_sources(feeds)
        assert articles == []

    entry = json.loads(
        (tmp_path / "fetch_failures.log").read_text(encoding="utf-8")
    )
    assert entry["source_name"] == "Unreachable"
    assert entry["error_type"] == "ConnectError"


def test_collect_sources_continues_after_failure(tmp_path, monkeypatch):
    """When one source fails, other sources are still collected."""
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    from app.pipeline.orchestrator import collect_sources

    feeds = FeedsConfig(
        topics=[],
        sources=[
            SourceConfig(name="Broken", type="rss",
                         url="https://broken.example.com/feed", topics=["AI"]),
            SourceConfig(name="Good", type="rss",
                         url="https://good.example.com/feed", topics=["AI"]),
        ],
    )

    good_article = Article(
        title="Working", content_md="body", url="https://good.example.com/a",
        source_name="Good", source_type="rss", published_at=None,
        images=[], raw_summary=None,
        fetched_at=datetime.now(timezone.utc),
    )

    exc = httpx.HTTPStatusError(
        "Not Found", request=httpx.Request("GET", "https://broken.example.com/feed"),
        response=httpx.Response(404, request=httpx.Request("GET", "https://broken.example.com/feed")),
    )

    def mock_fetch(src, proxy=None):
        if src.name == "Broken":
            raise exc
        return [good_article]

    with patch("app.pipeline.orchestrator._fetch_source", side_effect=mock_fetch):
        articles = collect_sources(feeds)
        assert len(articles) == 1
        assert articles[0].source_name == "Good"


def test_collect_sources_progress_called_on_failure(monkeypatch):
    """Progress callback receives failure message when source errors."""
    from app.pipeline.orchestrator import collect_sources

    feeds = FeedsConfig(
        topics=[],
        sources=[
            SourceConfig(name="FailSource", type="rss",
                         url="https://fail.example.com/feed", topics=["AI"]),
        ],
    )

    exc = httpx.HTTPStatusError(
        "Forbidden",
        request=httpx.Request("GET", "https://fail.example.com/feed"),
        response=httpx.Response(403, request=httpx.Request("GET", "https://fail.example.com/feed")),
    )

    messages = []

    def progress(msg):
        messages.append(msg)

    with patch("app.pipeline.orchestrator._fetch_source", side_effect=exc):
        collect_sources(feeds, progress=progress)

    # Should have both the "Fetching" and "Failed" messages
    assert any("FailSource" in m for m in messages)
    assert any("403" in m for m in messages)


def test_run_pipeline_handles_source_failure(tmp_path, monkeypatch):
    """Full pipeline run continues when a source fetch fails."""
    from app.pipeline.orchestrator import collect_sources, run_pipeline
    from app.llm.providers.mock import MockProvider

    cfg, store = build(tmp_path)
    feeds = FeedsConfig(
        topics=[Topic(name="AI", keywords=["GPT"])],
        sources=[
            SourceConfig(name="Bad", type="rss",
                         url="https://bad.example.com/feed", topics=["AI"]),
        ],
    )

    monkeypatch.setattr("app.pipeline.orchestrator.collect_sources",
                        lambda feeds_cfg, max_per_source=None, progress=None, workers=None, proxy=None: [])

    provider = MockProvider(responses=[
        json.dumps({"title_candidates": ["t"], "body_md": "b"}),
        json.dumps({"flagged_claims": []})])
    stats = run_pipeline(feeds, store, provider, max_drafts=5)
    # No articles fetched, but pipeline should not crash
    assert stats["fetched"] == 0
    assert stats["archived"] == 0


# ── stop button (SystemExit propagation from main thread) ────────────────


def test_collect_sources_stop_via_progress(tmp_path, monkeypatch):
    """When progress raises SystemExit (stop button), collect_sources propagates it."""
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    from app.pipeline.orchestrator import collect_sources

    feeds = FeedsConfig(
        topics=[],
        sources=[
            SourceConfig(name="SlowFeed", type="rss",
                         url="https://slow.example.com/feed", topics=["AI"]),
        ],
    )

    # Make _fetch_source hang for 60s to simulate a slow source
    def slow_fetch(src, proxy=None):
        time.sleep(60)
        return []

    # Progress callback that raises SystemExit on the second call
    # (first call = "抓取来源 [1/1]", second call = heartbeat "等待来源完成中…")
    call_count = [0]

    def stop_progress(msg):
        call_count[0] += 1
        if "等待来源完成中" in msg:
            raise SystemExit("stopped")

    with patch("app.pipeline.orchestrator._fetch_source", side_effect=slow_fetch):
        try:
            collect_sources(feeds, progress=stop_progress, source_timeout=30)
            assert False, "Should have raised SystemExit"
        except SystemExit as e:
            assert str(e) == "stopped"
    # Progress was called at least twice: fetch_one start + heartbeat
    assert call_count[0] >= 2


def test_collect_sources_heartbeat_on_main_thread(tmp_path, monkeypatch):
    """Heartbeat progress is called on the main thread every ~5s during waits."""
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    from app.pipeline.orchestrator import collect_sources

    feeds = FeedsConfig(
        topics=[],
        sources=[
            SourceConfig(name="SlowFeed", type="rss",
                         url="https://slow.example.com/feed", topics=["AI"]),
        ],
    )

    def slow_fetch(src, proxy=None):
        time.sleep(20)
        return []

    main_thread_id = threading.current_thread().ident
    heartbeat_thread_ids = []

    def track_thread(msg):
        # Only track heartbeat messages (called from main thread),
        # not fetch_one progress (called from worker threads)
        if "等待来源完成中" in msg:
            heartbeat_thread_ids.append(threading.current_thread().ident)

    with patch("app.pipeline.orchestrator._fetch_source", side_effect=slow_fetch):
        # Use short timeout to abort quickly
        collect_sources(feeds, progress=track_thread, source_timeout=6,
                        workers=1)

    # Heartbeat progress calls should all be on the main thread
    assert len(heartbeat_thread_ids) >= 1
    assert all(tid == main_thread_id for tid in heartbeat_thread_ids)


def test_collect_sources_stop_after_heartbeat(tmp_path, monkeypatch):
    """Stop takes effect within ~5 seconds via heartbeat, not source_timeout."""
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    from app.pipeline.orchestrator import collect_sources

    feeds = FeedsConfig(
        topics=[],
        sources=[
            SourceConfig(name="SlowFeed", type="rss",
                         url="https://slow.example.com/feed", topics=["AI"]),
        ],
    )

    def slow_fetch(src, proxy=None):
        time.sleep(120)  # very slow
        return []

    stop_at_call = [0]

    def progress_with_stop(msg):
        stop_at_call[0] += 1
        # Stop on the heartbeat call (not the initial "抓取来源" call)
        if "等待来源完成中" in msg:
            raise SystemExit("stopped")

    start = time.monotonic()
    with patch("app.pipeline.orchestrator._fetch_source", side_effect=slow_fetch):
        try:
            collect_sources(feeds, progress=progress_with_stop,
                            source_timeout=60)
        except SystemExit:
            pass
    elapsed = time.monotonic() - start
    # Stop should take effect within ~5-6 seconds, not 60
    assert elapsed < 15, f"Stop took {elapsed:.1f}s, expected < 15s"
