from pathlib import Path

from app.sources.rss import parse_feed

FIXTURE = Path(__file__).parent / "fixtures" / "sample_feed.xml"


def test_parse_feed_returns_articles():
    xml = FIXTURE.read_text(encoding="utf-8")
    articles = parse_feed(xml, source_name="Example AI Blog")
    assert len(articles) == 2
    first = articles[0]
    assert first.title == "New Model Released"
    assert first.url == "https://example.com/new-model"
    assert first.source_type == "rss"
    assert first.published_at is not None
    assert "summary" in first.raw_summary.lower()
