from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse, unquote, quote

import httpx

from app.feeds import SourceConfig

_UA = {"User-Agent": "media-agent/0.1 (+https://localhost)"}


def _get(url: str, fetch=None, timeout: float = 8.0) -> str:
    if fetch is not None:
        return fetch(url)
    resp = httpx.get(url, timeout=timeout, follow_redirects=True, headers=_UA)
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
    search_url = "https://html.duckduckgo.com/html/?q=" + quote(query)
    try:
        html = _get(search_url, fetch)
    except Exception:
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
