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

# Path segments that almost never point to an article.  ANY segment in this set
# causes the URL to be rejected — this catches /about-us/achievement-awards
# (where "about-us" matches) while keeping /blog-releases/release-notes.
#
# NOTE: terms like "events", "news", "blog" are deliberately NOT here —
# /events/ai-summit-2026 and /news/gpt-5 are valid articles.  They live in
# _NON_ARTICLE_TERMINAL instead, where only shallow index pages are blocked.
_NON_ARTICLE_SEGMENTS: set[str] = {
    "about", "about-us", "contact", "contact-us", "careers", "career",
    "jobs", "privacy", "terms", "tos", "legal", "policy", "policies",
    "cookie", "cookies", "pricing", "plans", "login", "signin", "sign-in",
    "signup", "sign-up", "register", "account", "subscribe", "newsletter",
    "sitemap", "search", "tag", "tags", "category", "categories",
    "author", "authors", "rss", "feed", "feeds", "faq", "faqs", "support",
    "help", "press", "press-kit", "media-kit", "brand", "security",
    "status", "download", "downloads", "products", "product", "solutions",
    "company", "team", "partners", "investors",
    "trust", "compliance", "responsible-disclosure",
    # Additional common non-article sections
    "awards", "award", "achievement", "achievements",
    "leadership", "management", "board", "governance",
    "affiliates", "services", "service",
    "locations", "offices", "office",
    "patents", "trademarks", "licensing",
    "recognition", "testimonials", "clients",
    "data-privacy", "gdpr", "ccpa",
    # Compound variants — exact-match set can't catch "terms-of-use" via "terms"
    "terms-of-use", "terms-of-service", "terms-and-conditions",
    "privacy-policy", "privacy-statement", "privacy-notice",
    "privacy-policies",
    "cookie-policy", "cookie-preferences", "cookie-settings",
    "code-of-conduct", "code-of-ethics",
    "return-policy", "refund-policy", "cancellation-policy",
    "anti-corruption", "anti-bribery", "anti-harassment",
    "community-guidelines",
    "data-processing-agreement", "service-level-agreement",
}

# Terms that indicate a blog / news / article INDEX page when they appear
# as the FINAL path segment AND the path is shallow (≤2 segments).
# /quantum/blog              → blocked (terminal "blog", depth 2)
# /quantum/blog/post-slug    → accepted (depth 3)
# /news                      → blocked (terminal "news", depth 1)
# /news-events/3gpp-news/release-18 → accepted (depth 3)
# /events/ai-summit-2026     → accepted (terminal "ai-summit-2026" not in set)
# /events                    → blocked (terminal "events", depth 1)
_NON_ARTICLE_TERMINAL: set[str] = {
    "blog", "news", "events", "event", "updates", "articles", "posts",
    "insights", "features", "stories", "publications", "bulletins", "digest",
    "archive", "archives", "media", "resources", "library",
    "newsroom", "media-center",
    "press-release", "press-releases",
}

# File extensions that are clearly not articles.
_ASSET_EXTS = {
    "pdf", "jpg", "jpeg", "png", "gif", "svg", "webp", "ico", "css", "js",
    "json", "zip", "gz", "tar", "mp4", "mp3", "wav", "mov", "avi", "woff",
    "woff2", "ttf", "eot",
}


def _is_non_article(absolute: str) -> bool:
    """Return True if *absolute* URL almost certainly does not point to an
    individual article page.

    Three-tier rejection:
      1. Asset file extension (*.pdf, *.jpg …).
      2. ANY path segment matches ``_NON_ARTICLE_SEGMENTS``.
      3. Terminal segment matches ``_NON_ARTICLE_TERMINAL`` **and** the
         path is shallow (≤2 segments deep).
    """
    path = urlparse(absolute).path.lower()
    segments = [s for s in path.split("/") if s]
    if not segments:
        return True  # bare domain / homepage

    last = segments[-1]

    # ── Tier 1: asset files ─────────────────────────────────────────
    if "." in last:
        ext = last.rsplit(".", 1)[-1]
        if ext in _ASSET_EXTS:
            return True

    # ── Tier 2: any segment matches a non‑article keyword ────────────
    # cat /about-us/achievement-awards  (about-us matches)
    # cat /careers/software-engineer    (careers matches)
    # keep /news-events/3gpp-news/release-18
    for seg in segments:
        if seg in _NON_ARTICLE_SEGMENTS:
            return True

    # ── Tier 3: terminal segment is a container / index page ────────
    # /quantum/blog               → blocked  (depth 2)
    # /blog                        → blocked  (depth 1)
    # /quantum/blog/post-slug      → accepted (depth 3)
    if last in _NON_ARTICLE_TERMINAL and len(segments) <= 2:
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


def _is_article_content(data: dict) -> bool:
    """Return False if extracted data looks like a list / landing / thin page
    rather than a real article.

    Checks:
      - Minimum content length (300 chars)
      - At least 3 non‑link text lines (i.e. not just a link index)
    """
    md = (data.get("content_md") or "").strip()
    if len(md) < 300:  # noqa: PLR2004
        return False
    lines = md.split("\n")
    text_lines = [
        l for l in lines
        if l.strip() and not l.strip().startswith("http")
        and not l.startswith("![")
    ]
    return len(text_lines) >= 3


def scrape_single(url: str, source_name: str, timeout: float = 20.0,
                  render_js: bool = False) -> Article | None:
    html = _fetch_html(url, render_js=render_js, timeout=timeout)
    data = extract_from_html(html, url=url)
    if not data["content_md"] or not _is_article_content(data):
        return None
    return Article(
        title=data["title"] or url,
        content_md=data["content_md"],
        url=url,
        source_name=source_name,
        source_type="scrape",
        published_at=data.get("published_at"),
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
    source_normalized = url.rstrip("/")
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
            if link in seen_links:
                continue
            if link.rstrip("/") == source_normalized:
                continue  # self-link back to the list page
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
