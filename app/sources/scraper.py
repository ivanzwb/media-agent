from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx

from app.models import Article
from app.sources.extractor import extract_from_html

_UA_STR = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_UA = {"User-Agent": _UA_STR}

# Match <a ... href=...> with double/single/unquoted values, case-insensitive.
_HREF_RE = re.compile(
    r'<a\b[^>]*?\bhref\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s">]+))', re.I)

# Pagination URL patterns (?page=N, /page/N, /p/N).
_PAGE_RE = re.compile(r'(?:[?&]page=\d+|/page/\d+|/p/\d+)', re.I)


def _hrefs(html: str) -> list[str]:
    out: list[str] = []
    for m in _HREF_RE.finditer(html):
        out.append(m.group(1) or m.group(2) or m.group(3) or "")
    return out

# Path segments that almost never point to an article. Matched as whole
# path segments (case-insensitive), so "/news/about-ai" is NOT blocked.
_NON_ARTICLE_SEGMENTS = {
    "about", "about-us", "contact", "contact-us", "careers", "career",
    "jobs", "privacy", "terms", "tos", "legal", "policy", "policies",
    "cookie", "cookies", "pricing", "plans", "login", "signin", "sign-in",
    "signup", "sign-up", "register", "account", "subscribe", "newsletter",
    "sitemap", "search", "tag", "tags", "category", "categories",
    "author", "authors", "rss", "feed", "feeds", "faq", "faqs", "support",
    "help", "press", "press-kit", "media-kit", "brand", "security",
    "status", "download", "downloads", "products", "product", "solutions",
    "company", "team", "events", "event", "partners", "investors",
    "trust", "compliance", "responsible-disclosure",
}

# File extensions that are clearly not articles.
_ASSET_EXTS = {
    "pdf", "jpg", "jpeg", "png", "gif", "svg", "webp", "ico", "css", "js",
    "json", "zip", "gz", "tar", "mp4", "mp3", "wav", "mov", "avi", "woff",
    "woff2", "ttf", "eot",
}


def _is_non_article(absolute: str) -> bool:
    path = urlparse(absolute).path.lower()
    segments = [s for s in path.split("/") if s]
    if not segments:
        return True  # bare domain / homepage, not an article
    last = segments[-1]
    # Asset files are never articles.
    if "." in last:
        ext = last.rsplit(".", 1)[-1]
        if ext in _ASSET_EXTS:
            return True
    # Only the LAST path segment decides: this blocks section landing pages
    # (/about, /careers, /news, /events) while keeping deeper article slugs
    # (/news/<slug>, /events/ai-summit-2026, /blog/download-whitepaper).
    if last in _NON_ARTICLE_SEGMENTS:
        return True
    return False


def discover_links(html: str, base_url: str,
                   include_pattern: str | None = None,
                   exclude_pattern: str | None = None) -> list[str]:
    base_host = urlparse(base_url).netloc
    hrefs = _hrefs(html)
    out: list[str] = []
    seen: set[str] = set()
    for href in hrefs:
        href = href.strip()
        if not href or href.startswith(("#", "mailto:", "javascript:",
                                        "tel:", "data:")):
            continue
        absolute = urljoin(base_url, href)
        if urlparse(absolute).netloc != base_host:
            continue
        if include_pattern and include_pattern not in absolute:
            continue
        if exclude_pattern and exclude_pattern in absolute:
            continue
        if _is_non_article(absolute):
            continue
        if absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


def _render_with_playwright(url: str, timeout: float) -> str | None:
    """Render a JS-heavy page with Playwright if it's installed; else None."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=_UA_STR)
            page.goto(url, wait_until="networkidle",
                      timeout=int(timeout * 1000))
            html = page.content()
            browser.close()
            return html
    except Exception:
        return None


def _fetch_html(url: str, render_js: bool = False,
                timeout: float = 20.0) -> str:
    """Fetch page HTML. With render_js, use Playwright (if available) for
    SPA/SSR sites, falling back to a plain httpx request."""
    if render_js:
        html = _render_with_playwright(url, timeout)
        if html:
            return html
    resp = httpx.get(url, timeout=timeout, follow_redirects=True, headers=_UA)
    resp.raise_for_status()
    return resp.text


def _pagination_links(html: str, base_url: str) -> list[str]:
    """Same-host links that look like pagination (?page=N, /page/N)."""
    host = urlparse(base_url).netloc
    out: list[str] = []
    seen: set[str] = set()
    for href in _hrefs(html):
        href = href.strip()
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        if urlparse(absolute).netloc != host:
            continue
        if _PAGE_RE.search(absolute) and absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


def scrape_single(url: str, source_name: str, timeout: float = 20.0,
                  render_js: bool = False) -> Article | None:
    html = _fetch_html(url, render_js=render_js, timeout=timeout)
    data = extract_from_html(html, url=url)
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
        videos=data.get("videos", []),
    )


def scrape_list(url: str, source_name: str, include_pattern: str | None = None,
                exclude_pattern: str | None = None,
                max_articles: int = 10, delay: float = 1.0,
                max_pages: int = 1, render_js: bool = False) -> list[Article]:
    # Discover article links across the list page and (optionally) its
    # paginated siblings, then scrape each discovered article.
    article_links: list[str] = []
    seen_links: set[str] = set()
    visited_pages: set[str] = set()
    queue: list[str] = [url]
    pages = 0
    max_pages = max(1, max_pages)

    while queue and pages < max_pages:
        page_url = queue.pop(0)
        if page_url in visited_pages:
            continue
        visited_pages.add(page_url)
        pages += 1
        try:
            html = _fetch_html(page_url, render_js=render_js)
        except Exception:
            continue
        for link in discover_links(html, base_url=page_url,
                                   include_pattern=include_pattern,
                                   exclude_pattern=exclude_pattern):
            if link not in seen_links:
                seen_links.add(link)
                article_links.append(link)
        if pages < max_pages:
            for nxt in _pagination_links(html, page_url):
                if nxt not in visited_pages and nxt not in queue:
                    queue.append(nxt)

    articles: list[Article] = []
    for link in article_links[:max_articles]:
        try:
            art = scrape_single(link, source_name, render_js=render_js)
            if art:
                articles.append(art)
        except Exception:
            continue
        time.sleep(delay)
    return articles
