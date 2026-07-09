from __future__ import annotations

import time
from datetime import datetime, timezone
from time import mktime

import feedparser
import httpx
import re
import trafilatura
from readability import Document

from urllib.parse import urljoin

from app.models import Article
from app.sources.date_parser import parse_date
from app.sources.extractor import _images_from_html, _videos_from_html

_UA_STR = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Rotated User-Agents — cycle through on retry to evade Cloudflare/WAF blocks.
_ROTATED_UAS = [
    _UA_STR,
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 Edg/124.0",
]

_RSS_RETRIES = 3  # total attempts = 4 (first + 3 retries)


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
                         headers={"User-Agent": _UA_STR})
        resp.raise_for_status()
        html = resp.text
    except Exception:
        return None

    if not html:
        return None

    # 1) trafilatura — best for article extraction
    try:
        extracted = trafilatura.extract(html, include_images=True,
                                        output_format="html",
                                        url=url)
        if extracted and len(extracted.strip()) > 200:
            # trafilatura outputs <graphic> instead of <img> — convert so the
            # downstream image extractor / downloader recognises them.
            extracted = re.sub(
                r'<graphic\s+src="([^"]+)"(?:\s+alt="([^"]*)")?\s*/?>',
                r'<img src="\1" alt="\2"/>', extracted)
            return extracted.strip()
    except Exception:
        pass

    # 2) readability-lxml fallback
    try:
        doc = Document(html)
        content = doc.summary()
        if content and len(content.strip()) > 200:
            return content.strip()
    except Exception:
        pass

    return None


def _ua_header(attempt: int) -> dict[str, str]:
    """Return a User-Agent header, rotating on retry to evade blocking."""
    idx = attempt % len(_ROTATED_UAS)
    return {"User-Agent": _ROTATED_UAS[idx]}


def fetch_feed(url: str, source_name: str, timeout: float = 20.0) -> list[Article]:
    """Fetch and parse an RSS/Atom feed, with retry+backoff on transient errors.

    Transient failures (timeouts, connection errors, 5xx, 429) are retried up
    to ``_RSS_RETRIES`` times with exponential backoff.  Permanent failures
    (4xx other than 429) are raised immediately without retrying.
    User-Agent is rotated on each retry attempt.
    """
    last_exc: Exception | None = None
    for attempt in range(_RSS_RETRIES + 1):
        try:
            resp = httpx.get(url, timeout=timeout, follow_redirects=True,
                             headers=_ua_header(attempt))
            resp.raise_for_status()
            return parse_feed(resp.text, source_name)
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            code = exc.response.status_code
            # 4xx (except 429) are permanent — don't waste retries on them.
            if code < 500 and code != 429:  # noqa: PLR2004
                raise
        except (httpx.TimeoutException, httpx.NetworkError, OSError) as exc:
            last_exc = exc
        except httpx.HTTPError as exc:
            # Other HTTP-level errors (RemoteProtocolError, DecodingError, etc.)
            last_exc = exc
        if attempt < _RSS_RETRIES:
            backoff = 0.5 * (2 ** attempt)  # 0.5s, 1s, 2s, …
            time.sleep(backoff)
    assert last_exc is not None
    raise last_exc
