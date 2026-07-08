from pathlib import Path
from unittest.mock import patch

from app.sources.rss import parse_feed, _fetch_full_article

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
            "<p>This is a long and detailed article body "
            "with enough content to pass the threshold check.</p>"
            + "x" * 200  # ensure > 200 chars
        )
        result = _fetch_full_article("https://example.com/article")
        assert result is not None
        assert "long and detailed" in result
        assert "trafilatura" not in result  # clean HTML


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
