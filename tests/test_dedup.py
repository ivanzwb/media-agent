from datetime import datetime, timezone

from app.models import Article
from app.sources.dedup import dedup


def art(url, title):
    return Article(title=title, content_md="b", url=url, source_name="s",
                   source_type="rss", published_at=None, images=[],
                   raw_summary=None, fetched_at=datetime.now(timezone.utc))


def test_dedup_removes_same_url_with_tracking():
    items = [art("https://x.com/a", "T"),
             art("https://x.com/a?utm_source=z", "T")]
    out = dedup(items)
    assert len(out) == 1


def test_dedup_keeps_distinct():
    items = [art("https://x.com/a", "A"), art("https://x.com/b", "B")]
    assert len(dedup(items)) == 2
