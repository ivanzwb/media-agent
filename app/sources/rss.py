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
from app.sources.scraper import render_with_playwright

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


def parse_feed(xml: str, source_name: str, enrich: bool = True,
               render_js: bool = True,
               proxy: str | None = None) -> list[Article]:
    """Parse an RSS/Atom feed and return a list of Articles.

    When *enrich* is True (the default), the parser fetches each article's
    original URL and attempts to extract the full body text from the web
    page.  If the extracted content is substantially longer than what the
    feed provided, the page text replaces the feed content.  This ensures
    articles from feeds that only carry short summaries (e.g. NVIDIA
    Developer Blog) still get the full body.

    When *render_js* is True, falls back to Playwright if the httpx request
    for enrichment fails (e.g. blocked by Cloudflare).

    If *proxy* is set (e.g. ``http://127.0.0.1:7890``), all HTTP calls go
    through that proxy — for bypassing Cloudflare/WAF or GFW blocks.
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
                full = _fetch_full_article(link, render_js=render_js,
                                           proxy=proxy)
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


def _fetch_url(url: str, timeout: float, render_js: bool,
               proxy: str | None = None) -> str | None:
    """Fetch a URL, trying httpx first; if that fails and *render_js* is
    True, fall back to Playwright (if available).

    Returns the response body as text, or None on failure.
    """
    # ① Try httpx with rotated User-Agents (4 attempts = 1 original + 3 retries).
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            ua = _ROTATED_UAS[attempt % len(_ROTATED_UAS)]
            if proxy:
                with httpx.Client(proxy=proxy, timeout=timeout,
                                  follow_redirects=True) as client:
                    resp = client.get(url, headers={"User-Agent": ua})
            else:
                resp = httpx.get(url, timeout=timeout,
                                 follow_redirects=True,
                                 headers={"User-Agent": ua})
            resp.raise_for_status()
            return resp.text
        except (httpx.HTTPStatusError, httpx.TimeoutException,
                httpx.ConnectError, httpx.NetworkError,
                httpx.HTTPError, Exception) as exc:
            last_exc = exc
            # Permanent 4xx (except 429) — don't retry.
            if isinstance(exc, httpx.HTTPStatusError):
                resp = exc.response
                if resp is not None and resp.status_code < 500 \
                        and resp.status_code != 429:
                    raise
        if attempt < 3:
            time.sleep(0.5 * (2 ** attempt))

    # ② Playwright fallback (httpx failed + render_js requested).
    if render_js and last_exc is not None:
        pw_html = render_with_playwright(url, timeout, wait_for=3000)
        if pw_html:
            return pw_html

    # httpx exhausted all retries and Playwright either wasn't requested or
    # also failed — re-raise the original exception so callers can distinguish
    # between "tried everything" and "no content" cases.
    if last_exc is not None:
        raise last_exc  # type: ignore[misc]
    return None


def _fetch_full_article(url: str, timeout: float = 30.0,
                        render_js: bool = True,
                        proxy: str | None = None) -> str | None:
    """Download the web page at *url* and extract its main text content.

    Returns the extracted content as HTML (preserving structure) or None on
    failure.  Uses trafilatura (primary) with readability-lxml as fallback.

    When *render_js* is True, falls back to Playwright if the httpx request
    fails (e.g. blocked by Cloudflare).

    If *proxy* is set (e.g. ``http://127.0.0.1:7890``), all HTTP calls go
    through that proxy — for bypassing Cloudflare/WAF or GFW blocks.
    """
    try:
        html = _fetch_url(url, timeout, render_js, proxy=proxy)
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


def fetch_feed(url: str, source_name: str, timeout: float = 30.0,
               render_js: bool = False,
               proxy: str | None = None) -> list[Article]:
    """Fetch and parse an RSS/Atom feed, with retry+backoff on transient errors.

    Transient failures (timeouts, connection errors, 5xx, 429) are retried up
    to ``_RSS_RETRIES`` times with exponential backoff.  Permanent failures
    (4xx other than 429) are raised immediately without retrying.
    User-Agent is rotated on each retry attempt.

    When *render_js* is True, falls back to Playwright (if installed) when
    httpx fails — useful for sites behind Cloudflare/WAF.

    If *proxy* is set (e.g. ``http://127.0.0.1:7890``), all HTTP calls go
    through that proxy — for bypassing Cloudflare/WAF or GFW blocks.
    """
    xml = _fetch_url(url, timeout, render_js, proxy=proxy)
    # _fetch_url raises on failure; xml is never None here (but guard for safety).
    if xml is None:  # pragma: no cover
        raise RuntimeError(
            f"Failed to fetch feed: {url} after all retries and fallbacks")
    return parse_feed(xml, source_name, render_js=render_js, proxy=proxy)
