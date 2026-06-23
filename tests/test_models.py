from datetime import datetime, timezone

from app.models import Article, ArticleStatus, DraftStatus, slugify


def test_article_fingerprint_stable():
    a = Article(title="Hello World", content_md="body", url="https://x.com/a?utm=1",
                source_name="X", source_type="rss",
                published_at=None, images=[], raw_summary=None,
                fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    b = Article(title="Hello   World", content_md="other", url="https://x.com/a",
                source_name="Y", source_type="scrape",
                published_at=None, images=[], raw_summary=None,
                fetched_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert a.fingerprint() == b.fingerprint()


def test_normalized_url_strips_tracking():
    a = Article(title="t", content_md="b", url="https://x.com/p?utm_source=rss&id=5",
                source_name="X", source_type="rss", published_at=None,
                images=[], raw_summary=None, fetched_at=datetime.now(timezone.utc))
    assert a.normalized_url() == "https://x.com/p?id=5"


def test_slugify():
    assert slugify("Hello, World! 2026") == "hello-world-2026"
    assert slugify("物理AI breakthrough")


def test_status_constants():
    assert ArticleStatus.ARCHIVED == "archived"
    assert DraftStatus.DRAFTED == "drafted"
