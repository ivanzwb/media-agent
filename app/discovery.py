from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse, unquote, quote

import httpx

from app.feeds import SourceConfig

_UA = {"User-Agent": "media-agent/0.1 (+https://localhost)"}


def _get(url: str, fetch=None) -> str:
    if fetch is not None:
        return fetch(url)
    resp = httpx.get(url, timeout=20.0, follow_redirects=True, headers=_UA)
    resp.raise_for_status()
    return resp.text


def _site_name(html: str, url: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    if m:
        title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if title:
            return title[:80]
    return urlparse(url).netloc or url


def find_feed_links(html: str, base_url: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"<link\b[^>]*>", html, re.IGNORECASE):
        tag = m.group(0)
        if not re.search(r'type=["\']application/(rss|atom)\+xml["\']',
                         tag, re.IGNORECASE):
            continue
        href = re.search(r'href=["\']([^"\']+)["\']', tag, re.IGNORECASE)
        if not href:
            continue
        absolute = urljoin(base_url, href.group(1))
        if absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


def discover_from_url(url: str, topics: list[str] | None = None,
                      fetch=None) -> list[SourceConfig]:
    topics = topics or []
    try:
        html = _get(url, fetch)
    except Exception:
        return []
    name = _site_name(html, url)
    feeds = find_feed_links(html, url)
    if feeds:
        return [SourceConfig(name=name, type="rss", url=f, topics=topics)
                for f in feeds]
    return [SourceConfig(name=name, type="scrape", url=url, topics=topics,
                         mode="list")]


def search_web(query: str, max_results: int = 5, fetch=None) -> list[str]:
    """Search the web via DuckDuckGo HTML (primary) with Bing fallback.

    Uses a circuit breaker: if DuckDuckGo fails, it is skipped for the
    rest of the process lifetime to avoid burning 8+ s per query.
    """
    results = _search_ddg(query, max_results, fetch)
    if not results:
        results = _search_bing(query, max_results, fetch)
    return results


# Circuit breaker: set to True after DDG fails once (process lifetime).
_ddg_down: bool = False


def _search_ddg(query: str, max_results: int, fetch=None) -> list[str]:
    """DuckDuckGo HTML scraping (fast, no API key)."""
    global _ddg_down  # noqa: PLW0603
    if _ddg_down:
        return []
    search_url = "https://html.duckduckgo.com/html/?q=" + quote(query)
    try:
        if fetch is not None:
            html = fetch(search_url)
        else:
            resp = httpx.get(search_url, timeout=8.0, follow_redirects=True,
                             headers=_UA)
            resp.raise_for_status()
            html = resp.text
    except Exception:
        _ddg_down = True
        return []
    results: list[str] = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"',
                         html, re.IGNORECASE):
        href = m.group(1)
        redirect = re.search(r"uddg=([^&]+)", href)
        if redirect:
            href = unquote(redirect.group(1))
        if href.startswith("http") and href not in results:
            results.append(href)
        if len(results) >= max_results:
            break
    if not results:
        _ddg_down = True
    return results


# Domains to exclude from Bing results (Bing internal + common junk)
_BING_SKIP = re.compile(
    r"(?:bing\.com|microsoft\.com|go\.microsoft\.com|"
    r"google\.com|googleapis\.com|gstatic\.com|"
    r"facebook\.com|twitter\.com|youtube\.com|"
    r"javascript:|mailto:|#)",
    re.IGNORECASE,
)


def _search_bing(query: str, max_results: int, fetch=None) -> list[str]:
    """Bing web scraping as fallback when DuckDuckGo is unavailable."""
    bing_ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/131.0.0.0 Safari/537.36")
    headers = {"User-Agent": bing_ua}
    try:
        resp = httpx.get(
            "https://www.bing.com/search",
            params={"q": query},
            timeout=10.0,
            follow_redirects=True,
            headers=headers,
        )
        resp.raise_for_status()
    except Exception:
        return []
    results: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'href="(https?://[^"]+)"', resp.text):
        url = unquote(m.group(1))
        if _BING_SKIP.search(url) or url in seen:
            continue
        seen.add(url)
        results.append(url)
        if len(results) >= max_results:
            break
    return results


def discover_from_keyword(keyword: str, topics: list[str] | None = None,
                          max_sites: int = 3, fetch=None) -> list[SourceConfig]:
    topics = topics or []
    urls = search_web(keyword, fetch=fetch)
    out: list[SourceConfig] = []
    seen: set[str] = set()
    for site in urls[:max_sites]:
        for src in discover_from_url(site, topics=topics, fetch=fetch):
            if src.url not in seen:
                seen.add(src.url)
                out.append(src)
    return out
