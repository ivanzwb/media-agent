"""Article-URL discovery via web standards (RSS/Atom autodiscovery + sitemaps).

These are *generic*, JS-free ways to find article URLs on arbitrary sites —
used as a fallback when HTML link scraping comes up empty (common on
JS-rendered list pages). News sitemaps and feeds also tend to carry reliable
publish dates, which helps the date-extraction problem too.

All network access is injected via a ``fetch(url) -> str`` callable so the
logic is unit-testable without real HTTP.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

_LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.I)
_REL_ALT_RE = re.compile(r'\brel=["\']?[^"\'>]*\balternate\b', re.I)
_FEED_TYPE_RE = re.compile(r'\btype=["\']application/(?:rss|atom)\+xml', re.I)
_HREF_RE = re.compile(r'\bhref=["\']([^"\']+)["\']', re.I)
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
                  "/news-sitemap.xml", "/sitemap-news.xml")


def find_feed_urls(html: str, base_url: str) -> list[str]:
    """Return RSS/Atom feed URLs declared in a page via
    ``<link rel="alternate" type="application/rss+xml" href=...>``."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _LINK_TAG_RE.finditer(html or ""):
        tag = m.group(0)
        if not _REL_ALT_RE.search(tag) or not _FEED_TYPE_RE.search(tag):
            continue
        hm = _HREF_RE.search(tag)
        if not hm:
            continue
        full = urljoin(base_url, hm.group(1))
        if full not in seen:
            seen.add(full)
            out.append(full)
    return out


def feed_entry_links(xml: str) -> list[str]:
    """Article links from an RSS/Atom feed body (parsed with feedparser)."""
    import feedparser
    feed = feedparser.parse(xml)
    out: list[str] = []
    seen: set[str] = set()
    for entry in feed.entries:
        link = getattr(entry, "link", None)
        if link and link not in seen:
            seen.add(link)
            out.append(link)
    return out


def sitemap_urls(base_url: str, fetch, include_pattern: str | None = None,
                 limit: int = 300, max_maps: int = 12) -> list[str]:
    """Collect page URLs from a site's sitemap(s).

    Honours ``Sitemap:`` directives in robots.txt, tries the common sitemap
    paths, follows one level of <sitemapindex>, and applies an optional
    substring ``include_pattern``. ``fetch(url) -> str`` must raise on failure.
    """
    parsed = urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"

    candidates: list[str] = []
    try:
        robots = fetch(urljoin(root, "/robots.txt"))
        for m in re.finditer(r"(?im)^\s*sitemap:\s*(\S+)", robots or ""):
            candidates.append(m.group(1).strip())
    except Exception:  # noqa: BLE001
        pass
    candidates += [urljoin(root, p) for p in _SITEMAP_PATHS]

    queue = list(dict.fromkeys(candidates))
    seen_maps: set[str] = set()
    out: list[str] = []
    seen: set[str] = set()
    maps_fetched = 0

    while queue and len(out) < limit and maps_fetched < max_maps:
        sm = queue.pop(0)
        if sm in seen_maps:
            continue
        seen_maps.add(sm)
        try:
            xml = fetch(sm)
        except Exception:  # noqa: BLE001
            continue
        maps_fetched += 1
        locs = _LOC_RE.findall(xml or "")
        if re.search(r"<sitemapindex", xml or "", re.I):
            # index of sub-sitemaps — queue them (one level).
            for loc in locs:
                if loc not in seen_maps and loc not in queue:
                    queue.append(loc)
            continue
        for loc in locs:
            if include_pattern and include_pattern not in loc:
                continue
            if loc not in seen:
                seen.add(loc)
                out.append(loc)
                if len(out) >= limit:
                    break
    return out
