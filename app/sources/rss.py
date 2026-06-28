from __future__ import annotations

from datetime import datetime, timezone
from time import mktime

import feedparser
import httpx

from urllib.parse import urljoin

from app.models import Article
from app.sources.date_parser import parse_date
from app.sources.extractor import _images_from_html, _videos_from_html


def _to_dt(entry) -> datetime | None:
    parsed = getattr(entry, "published_parsed", None) or \
        getattr(entry, "updated_parsed", None)
    if parsed:
        return datetime.fromtimestamp(mktime(parsed), tz=timezone.utc)
    # Fallback: try parsing the plain-text date fields.
    text = getattr(entry, "published", None) or \
        getattr(entry, "updated", None)
    if text:
        return parse_date(text)
    return None


def parse_feed(xml: str, source_name: str) -> list[Article]:
    feed = feedparser.parse(xml)
    now = datetime.now(timezone.utc)
    articles: list[Article] = []
    for entry in feed.entries:
        summary = getattr(entry, "summary", None)
        content_md = ""
        if getattr(entry, "content", None):
            content_md = entry.content[0].get("value", "")
        body_html = content_md or summary or ""
        link = getattr(entry, "link", "")
        articles.append(Article(
            title=getattr(entry, "title", "").strip(),
            content_md=body_html,
            url=link,
            source_name=source_name,
            source_type="rss",
            published_at=_to_dt(entry),
            images=[urljoin(link, u) for u in _images_from_html(body_html)],
            raw_summary=summary,
            fetched_at=now,
            videos=_videos_from_html(body_html, base_url=link),
        ))
    return articles


def fetch_feed(url: str, source_name: str, timeout: float = 20.0) -> list[Article]:
    resp = httpx.get(url, timeout=timeout, follow_redirects=True,
                     headers={"User-Agent": "media-agent/0.1"})
    resp.raise_for_status()
    return parse_feed(resp.text, source_name)
