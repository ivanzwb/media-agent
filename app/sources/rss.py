from __future__ import annotations

from datetime import datetime, timezone
from time import mktime

import feedparser
import httpx

from app.models import Article


def _to_dt(entry) -> datetime | None:
    parsed = getattr(entry, "published_parsed", None) or \
        getattr(entry, "updated_parsed", None)
    if not parsed:
        return None
    return datetime.fromtimestamp(mktime(parsed), tz=timezone.utc)


def parse_feed(xml: str, source_name: str) -> list[Article]:
    feed = feedparser.parse(xml)
    now = datetime.now(timezone.utc)
    articles: list[Article] = []
    for entry in feed.entries:
        summary = getattr(entry, "summary", None)
        content_md = ""
        if getattr(entry, "content", None):
            content_md = entry.content[0].get("value", "")
        articles.append(Article(
            title=getattr(entry, "title", "").strip(),
            content_md=content_md or summary or "",
            url=getattr(entry, "link", ""),
            source_name=source_name,
            source_type="rss",
            published_at=_to_dt(entry),
            images=[],
            raw_summary=summary,
            fetched_at=now,
        ))
    return articles


def fetch_feed(url: str, source_name: str, timeout: float = 20.0) -> list[Article]:
    resp = httpx.get(url, timeout=timeout, follow_redirects=True,
                     headers={"User-Agent": "media-agent/0.1"})
    resp.raise_for_status()
    return parse_feed(resp.text, source_name)
