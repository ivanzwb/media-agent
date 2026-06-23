from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx

from app.models import Article
from app.sources.extractor import extract_from_html

_UA = {"User-Agent": "media-agent/0.1 (+https://localhost)"}


def discover_links(html: str, base_url: str,
                   include_pattern: str | None = None) -> list[str]:
    base_host = urlparse(base_url).netloc
    hrefs = re.findall(r'<a[^>]+href="([^"]+)"', html)
    out: list[str] = []
    seen: set[str] = set()
    for href in hrefs:
        if href.startswith("#") or href.startswith("mailto:"):
            continue
        absolute = urljoin(base_url, href)
        if urlparse(absolute).netloc != base_host:
            continue
        if include_pattern and include_pattern not in absolute:
            continue
        if absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


def scrape_single(url: str, source_name: str,
                  timeout: float = 20.0) -> Article | None:
    resp = httpx.get(url, timeout=timeout, follow_redirects=True, headers=_UA)
    resp.raise_for_status()
    data = extract_from_html(resp.text, url=url)
    if not data["content_md"]:
        return None
    return Article(
        title=data["title"] or url,
        content_md=data["content_md"],
        url=url,
        source_name=source_name,
        source_type="scrape",
        published_at=None,
        images=data["images"],
        raw_summary=None,
        fetched_at=datetime.now(timezone.utc),
    )


def scrape_list(url: str, source_name: str, include_pattern: str | None = None,
                max_articles: int = 10, delay: float = 1.0) -> list[Article]:
    resp = httpx.get(url, timeout=20.0, follow_redirects=True, headers=_UA)
    resp.raise_for_status()
    links = discover_links(resp.text, base_url=url,
                           include_pattern=include_pattern)[:max_articles]
    articles: list[Article] = []
    for link in links:
        try:
            art = scrape_single(link, source_name)
            if art:
                articles.append(art)
        except Exception:
            continue
        time.sleep(delay)
    return articles
