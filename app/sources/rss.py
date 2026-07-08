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


def parse_feed(xml: str, source_name: str, enrich: bool = True) -> list[Article]:
    """Parse an RSS/Atom feed and return a list of Articles.

    When *enrich* is True (the default), the parser fetches each article's
    original URL and attempts to extract the full body text from the web
    page.  If the extracted content is substantially longer than what the
    feed provided, the page text replaces the feed content.  This ensures
    articles from feeds that only carry short summaries (e.g. NVIDIA
    Developer Blog) still get the full body.
    """
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
        # ── enrich: try to get full article from the original URL ──────
        if enrich and link:
            try:
                full = _fetch_full_article(link)
                if full and len(full) > len(body_html) * 1.5:
                    body_html = full
            except Exception:
                pass  # non-critical — keep feed content

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


def _fetch_full_article(url: str, timeout: float = 15.0) -> str | None:
    """Download the web page at *url* and extract its main text content.

    Returns the extracted content as HTML (preserving structure) or None on
    failure.  Uses trafilatura (primary) with readability-lxml as fallback.
    """
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True,
                         headers={"User-Agent": "media-agent/0.1"})
        resp.raise_for_status()
        html = resp.text
    except Exception:
        return None

    if not html:
        return None

    # 1) trafilatura — best for article extraction
    try:
        import trafilatura
        extracted = trafilatura.extract(html, include_images=True,
                                        output_format="html",
                                        url=url)
        if extracted and len(extracted.strip()) > 200:
            return extracted.strip()
    except Exception:
        pass

    # 2) readability-lxml fallback
    try:
        from readability import Document
        doc = Document(html)
        content = doc.summary()
        if content and len(content.strip()) > 200:
            return content.strip()
    except Exception:
        pass

    return None


def fetch_feed(url: str, source_name: str, timeout: float = 20.0) -> list[Article]:
    resp = httpx.get(url, timeout=timeout, follow_redirects=True,
                     headers={"User-Agent": "media-agent/0.1"})
    resp.raise_for_status()
    return parse_feed(resp.text, source_name)
