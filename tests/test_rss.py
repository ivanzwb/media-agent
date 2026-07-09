from pathlib import Path
from unittest.mock import patch

from app.sources.rss import parse_feed, fetch_feed, _fetch_full_article, _UA_STR

FIXTURE = Path(__file__).parent / "fixtures" / "sample_feed.xml"


def test_parse_feed_returns_articles():
    """Basic RSS parsing (enrich disabled to avoid HTTP calls in unit tests)."""
    xml = FIXTURE.read_text(encoding="utf-8")
    articles = parse_feed(xml, source_name="Example AI Blog", enrich=False)
    assert len(articles) == 2
    first = articles[0]
    assert first.title == "New Model Released"
    assert first.url == "https://example.com/new-model"
    assert first.source_type == "rss"
    assert first.published_at is not None
    assert "summary" in first.raw_summary.lower()


# ── _fetch_full_article ──────────────────────────────────────────────────

@patch("app.sources.rss.httpx.get")
def test_fetch_full_article_trafilatura_success(mock_get):
    """When trafilatura extracts meaningful content, use it."""
    mock_get.return_value.text = "<html><body><p>short</p></body></html>"
    mock_get.return_value.raise_for_status = lambda: None

    with patch("app.sources.rss.trafilatura") as mock_traf:
        mock_traf.extract.return_value = (
            "<p>Article body with image:</p>"
            + '<graphic src="https://example.com/img1.webp" alt="diagram"/>'
            + "<p>More text after image.</p>"
            + "x" * 200  # ensure > 200 chars
        )
        result = _fetch_full_article("https://example.com/article")
        assert result is not None
        assert "img1.webp" in result
        # graphic tag should be converted to img
        assert "<graphic" not in result
        assert '<img src="https://example.com/img1.webp"' in result
        assert 'alt="diagram"' in result


@patch("app.sources.rss.httpx.get")
def test_fetch_full_article_graphic_no_alt(mock_get):
    """graphic tag without alt attribute still converts to img."""
    mock_get.return_value.text = "<html></html>"
    mock_get.return_value.raise_for_status = lambda: None
    with patch("app.sources.rss.trafilatura") as mock_traf:
        mock_traf.extract.return_value = (
            '<graphic src="https://example.com/pic.png"/>'
            + "x" * 200
        )
        result = _fetch_full_article("https://example.com/article")
        assert "<graphic" not in result
        assert '<img src="https://example.com/pic.png"' in result


@patch("app.sources.rss.httpx.get")
def test_fetch_full_article_multiple_graphics(mock_get):
    """Multiple graphic tags all converted."""
    mock_get.return_value.text = "<html></html>"
    mock_get.return_value.raise_for_status = lambda: None
    with patch("app.sources.rss.trafilatura") as mock_traf:
        mock_traf.extract.return_value = (
            '<graphic src="https://example.com/a.webp"/>'
            '<graphic src="https://example.com/b.gif" alt="screenshot"/>'
            + "x" * 200
        )
        result = _fetch_full_article("https://example.com/article")
        assert result.count("<img") == 2
        assert "<graphic" not in result
        assert "a.webp" in result
        assert "b.gif" in result


@patch("app.sources.rss.httpx.get")
def test_fetch_full_article_preserves_code_blocks(mock_get):
    """Pre blocks from trafilatura output are preserved."""
    mock_get.return_value.text = "<html></html>"
    mock_get.return_value.raise_for_status = lambda: None
    with patch("app.sources.rss.trafilatura") as mock_traf:
        mock_traf.extract.return_value = (
            "<p>Here is some code:</p>"
            "<pre>import os\nprint('hello')</pre>"
            "<p>End.</p>"
            + "x" * 200
        )
        result = _fetch_full_article("https://example.com/article")
        assert "<pre>" in result
        assert "import os" in result
        assert "print('hello')" in result


@patch("app.sources.rss.httpx.get")
def test_fetch_full_article_readability_fallback(mock_get):
    """When trafilatura fails, fall back to readability-lxml."""
    mock_get.return_value.text = "<html><body><p>content</p></body></html>"
    mock_get.return_value.raise_for_status = lambda: None

    with patch("app.sources.rss.trafilatura.extract",
               side_effect=Exception("extract failed")):
        with patch("app.sources.rss.Document") as mock_doc:
            instance = mock_doc.return_value
            instance.summary.return_value = (
                "<p>Readability extracted content here</p>" + "y" * 200
            )
            result = _fetch_full_article("https://example.com/article")
            assert result is not None
            assert "Readability extracted" in result


@patch("app.sources.rss.httpx.get")
def test_fetch_full_article_too_short(mock_get):
    """Content below 200 chars is rejected."""
    mock_get.return_value.text = "<html><body><p>tiny</p></body></html>"
    mock_get.return_value.raise_for_status = lambda: None

    with patch("app.sources.rss.trafilatura") as mock_traf:
        mock_traf.extract.return_value = "<p>too short</p>"
        result = _fetch_full_article("https://example.com/article")
        assert result is None


@patch("app.sources.rss.httpx.get")
def test_fetch_full_article_http_failure(mock_get):
    """HTTP error returns None."""
    mock_get.side_effect = Exception("Connection refused")
    result = _fetch_full_article("https://example.com/article")
    assert result is None


@patch("app.sources.rss.httpx.get")
def test_fetch_full_article_both_fail(mock_get):
    """Both trafilatura and readability fail → None."""
    mock_get.return_value.text = "<html><body><p>x</p></body></html>"
    mock_get.return_value.raise_for_status = lambda: None

    with patch("app.sources.rss.trafilatura.extract",
               side_effect=Exception("fail")):
        with patch("app.sources.rss.Document",
                   side_effect=Exception("readability fail")):
            result = _fetch_full_article("https://example.com/article")
            assert result is None


# ── parse_feed with enrich ──────────────────────────────────────────────

def test_parse_feed_with_enrich_replaces_short_summary():
    """When the scraped full article is significantly longer, use it."""
    xml = FIXTURE.read_text(encoding="utf-8")
    # Mock _fetch_full_article to return a much longer body
    long_content = (
        "<h1>Full Article</h1>"
        + "<p>This is the complete article body with detailed content.</p>" * 20
    )
    with patch("app.sources.rss._fetch_full_article", return_value=long_content):
        articles = parse_feed(xml, source_name="Test Blog", enrich=True)
        assert len(articles) == 2
        # First article's summary was short → should be replaced
        assert "Full Article" in articles[0].content_md
        assert articles[0].content_md == long_content
        assert "Full Article" in articles[1].content_md


def test_parse_feed_with_enrich_keeps_when_shorter():
    """When scraped content is NOT significantly longer, keep the feed content."""
    xml = FIXTURE.read_text(encoding="utf-8")
    short_scraped = "<p>even shorter</p>"
    with patch("app.sources.rss._fetch_full_article", return_value=short_scraped):
        articles = parse_feed(xml, source_name="Test Blog", enrich=True)
        assert len(articles) == 2
        # Feed had a summary; scraped content was shorter → keep feed content
        assert "summary" in articles[0].content_md.lower()
        assert articles[0].content_md != short_scraped


def test_parse_feed_with_enrich_handles_fetch_failure():
    """When fetch fails for all articles, keep feed content gracefully."""
    xml = FIXTURE.read_text(encoding="utf-8")
    with patch("app.sources.rss._fetch_full_article", return_value=None):
        articles = parse_feed(xml, source_name="Test Blog", enrich=True)
        assert len(articles) == 2
        # Should fall back to feed summary
        assert articles[0].raw_summary is not None
        assert "summary" in articles[0].content_md.lower()


def test_parse_feed_enrich_disabled():
    """With enrich=False, original behavior is preserved."""
    xml = FIXTURE.read_text(encoding="utf-8")
    with patch("app.sources.rss._fetch_full_article") as mock_fetch:
        articles = parse_feed(xml, source_name="Test Blog", enrich=False)
        mock_fetch.assert_not_called()
        assert len(articles) == 2
        assert "summary" in articles[0].content_md.lower()


# ── UA header ─────────────────────────────────────────────────────────────


@patch("app.sources.rss.httpx.get")
def test_fetch_feed_uses_chrome_ua(mock_get):
    """fetch_feed passes a Chrome User-Agent, not a bot-like one."""
    mock_get.return_value.text = ""
    mock_get.return_value.raise_for_status = lambda: None
    # parse_feed needs valid XML, so mock it too
    with patch("app.sources.rss.parse_feed", return_value=[]):
        fetch_feed("https://example.com/feed", "Test")
    _, kwargs = mock_get.call_args
    ua = kwargs["headers"]["User-Agent"]
    assert "Chrome" in ua
    assert "Mozilla/5.0" in ua
    assert "media-agent" not in ua


@patch("app.sources.rss.httpx.get")
def test_fetch_full_article_uses_chrome_ua(mock_get):
    """_fetch_full_article passes a Chrome User-Agent, not a bot-like one."""
    mock_get.return_value.text = "<html></html>"
    mock_get.return_value.raise_for_status = lambda: None
    with patch("app.sources.rss.trafilatura.extract",
               side_effect=Exception("no trafilatura")):
        with patch("app.sources.rss.Document",
                   side_effect=Exception("no readability")):
            _fetch_full_article("https://example.com/article")
    _, kwargs = mock_get.call_args
    ua = kwargs["headers"]["User-Agent"]
    assert "Chrome" in ua
    assert "media-agent" not in ua


@patch("app.sources.rss.httpx.get")
def test_fetch_feed_ua_not_bot_string(mock_get):
    """The UA is the real Chrome string, not the old 'media-agent/0.1'."""
    mock_get.return_value.text = ""
    mock_get.return_value.raise_for_status = lambda: None
    with patch("app.sources.rss.parse_feed", return_value=[]):
        fetch_feed("https://example.com/feed", "Test")
    _, kwargs = mock_get.call_args
    ua = kwargs["headers"]["User-Agent"]
    assert ua == _UA_STR  # noqa: SLF001 — testing module constant


def test_parse_feed_enrich_mixed_results():
    """One article gets enriched, one doesn't."""
    xml = FIXTURE.read_text(encoding="utf-8")
    long_content = "<p>Rich body" + "x" * 500 + "</p>"

    def side_effect(url):
        if "new-model" in url:
            return long_content  # enrich first article
        return None             # second article fetch fails

    with patch("app.sources.rss._fetch_full_article", side_effect=side_effect):
        articles = parse_feed(xml, source_name="Test Blog", enrich=True)
        assert len(articles) == 2
        # First: enriched
        assert articles[0].content_md == long_content
        # Second: kept feed summary
        assert "embodied" in articles[1].content_md.lower()


# ── fetch_feed retry + UA rotation ────────────────────────────────────


def test_fetch_feed_retries_timeout_then_succeeds(monkeypatch):
    """Timeout on first 2 attempts, succeeds on 3rd."""
    import httpx
    from app.sources import rss
    monkeypatch.setattr(rss.time, "sleep", lambda *_: None)
    monkeypatch.setattr(rss, "parse_feed", lambda *a, **k: [])
    calls = {"n": 0}

    def flaky_get(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.TimeoutException("boom")
        class R:
            text = "OK"
            def raise_for_status(self): ...
        return R()
    monkeypatch.setattr(rss.httpx, "get", flaky_get)
    result = rss.fetch_feed("https://example.com/feed", "Test")
    assert result == []
    assert calls["n"] == 3  # 2 failures + 1 success


def test_fetch_feed_retries_429_then_succeeds(monkeypatch):
    """429 Too Many Requests is retried, not treated as permanent."""
    import httpx
    from app.sources import rss
    monkeypatch.setattr(rss.time, "sleep", lambda *_: None)
    monkeypatch.setattr(rss, "parse_feed", lambda *a, **k: [])
    calls = {"n": 0}
    req = httpx.Request("GET", "https://example.com/feed")

    def rate_limit(*a, **k):
        calls["n"] += 1
        if calls["n"] < 2:
            resp = httpx.Response(429, request=req)
            raise httpx.HTTPStatusError("429", request=req, response=resp)
        class R:
            text = "OK"
            def raise_for_status(self): ...
        return R()
    monkeypatch.setattr(rss.httpx, "get", rate_limit)
    result = rss.fetch_feed("https://example.com/feed", "Test")
    assert result == []
    assert calls["n"] == 2  # 1 failure + 1 success


def test_fetch_feed_no_retry_on_4xx(monkeypatch):
    """403/404/410 are permanent — raise immediately, no retries."""
    import pytest
    import httpx
    from app.sources import rss
    monkeypatch.setattr(rss.time, "sleep", lambda *_: None)
    monkeypatch.setattr(rss, "parse_feed", lambda *a, **k: [])
    calls = {"n": 0}
    req = httpx.Request("GET", "https://example.com/feed")

    def get403(*a, **k):
        calls["n"] += 1
        resp = httpx.Response(403, request=req)
        raise httpx.HTTPStatusError("403", request=req, response=resp)
    monkeypatch.setattr(rss.httpx, "get", get403)
    with pytest.raises(httpx.HTTPStatusError):
        rss.fetch_feed("https://example.com/feed", "Test")
    assert calls["n"] == 1  # 4xx is permanent — no retries


def test_fetch_feed_retries_5xx(monkeypatch):
    """500/502/503 are transient — retry until exhausted."""
    import pytest
    import httpx
    from app.sources import rss
    monkeypatch.setattr(rss.time, "sleep", lambda *_: None)
    monkeypatch.setattr(rss, "parse_feed", lambda *a, **k: [])
    calls = {"n": 0}
    req = httpx.Request("GET", "https://example.com/feed")

    def server_error(*a, **k):
        calls["n"] += 1
        resp = httpx.Response(502, request=req)
        raise httpx.HTTPStatusError("502", request=req, response=resp)
    monkeypatch.setattr(rss.httpx, "get", server_error)
    with pytest.raises(httpx.HTTPStatusError):
        rss.fetch_feed("https://example.com/feed", "Test")
    # _RSS_RETRIES=3 → 4 total attempts (1 original + 3 retries)
    assert calls["n"] == 4


def test_fetch_feed_rotates_ua_on_retry(monkeypatch):
    """Each retry attempt uses a different User-Agent."""
    import httpx
    from app.sources import rss
    monkeypatch.setattr(rss.time, "sleep", lambda *_: None)
    monkeypatch.setattr(rss, "parse_feed", lambda *a, **k: [])
    uas = []

    def record_ua(*a, **k):
        uas.append(k.get("headers", {}).get("User-Agent", ""))
        raise httpx.TimeoutException("boom")
    monkeypatch.setattr(rss.httpx, "get", record_ua)

    try:
        rss.fetch_feed("https://example.com/feed", "Test")
    except httpx.TimeoutException:
        pass

    assert len(uas) == 4  # 4 attempts
    # First attempt should use the default Chrome UA
    assert "Chrome" in uas[0]
    # At least one subsequent attempt should use a different UA
    assert len(set(uas)) >= 2  # at least 2 unique UAs


def test_fetch_feed_retries_connection_error(monkeypatch):
    """ConnectError (DNS/unreachable) is transient — retry."""
    import httpx
    from app.sources import rss
    monkeypatch.setattr(rss.time, "sleep", lambda *_: None)
    monkeypatch.setattr(rss, "parse_feed", lambda *a, **k: [])
    calls = {"n": 0}

    def connect_error(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("no route")
        class R:
            text = "OK"
            def raise_for_status(self): ...
        return R()
    monkeypatch.setattr(rss.httpx, "get", connect_error)
    result = rss.fetch_feed("https://example.com/feed", "Test")
    assert result == []
    assert calls["n"] == 3


def test_fetch_feed_backoff_increases(monkeypatch):
    """Sleep time doubles each retry: 0.5s, 1s, 2s."""
    import httpx
    from app.sources import rss
    sleeps = []
    monkeypatch.setattr(rss.time, "sleep", sleeps.append)
    monkeypatch.setattr(rss, "parse_feed", lambda *a, **k: [])
    req = httpx.Request("GET", "https://example.com/feed")

    def flaky_5xx(*a, **k):
        resp = httpx.Response(503, request=req)
        raise httpx.HTTPStatusError("503", request=req, response=resp)
    monkeypatch.setattr(rss.httpx, "get", flaky_5xx)

    try:
        rss.fetch_feed("https://example.com/feed", "Test")
    except httpx.HTTPStatusError:
        pass

    assert len(sleeps) == 3
    assert sleeps[0] == 0.5
    assert sleeps[1] == 1.0
    assert sleeps[2] == 2.0
