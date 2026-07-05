from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx

from app.models import Article
from app.sources import feed_discovery
from app.sources.extractor import extract_from_html

logger = logging.getLogger(__name__)

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

# Two-tier denylist of path segments that don't point to an article.
#
# WHY TWO TIERS: a single "reject if ANY segment matches" rule was both too
# greedy and too weak.  It dropped real articles like /company/blog/announce-x
# (because "company" matched) while a deeply-nested article under an org
# section was indistinguishable from a person/landing page.  Splitting fixes
# this:
#
#   _HARD_NON_ARTICLE  — legal / auth / account / commerce pages that are NEVER
#       an article, even when deeply nested.  Rejected at ANY depth.
#       e.g. /help/legal/terms-of-use  →  rejected
#
#   _SOFT_NON_ARTICLE  — organizational sections that are usually landing pages
#       but often PARENT real articles.  Rejected only on shallow paths (≤2
#       segments); deeper URLs are presumed articles and kept.
#       e.g. /about-us/team            →  rejected (depth 2)
#            /company/blog/announce-x  →  kept     (depth 3)
#
# NOTE: terms like "events", "news", "blog" live in _NON_ARTICLE_TERMINAL, not
# here — /news/gpt-5 is a valid article; only bare index pages are blocked.
_HARD_NON_ARTICLE: set[str] = {
    "privacy", "terms", "tos", "legal", "login", "signin", "sign-in",
    "signup", "sign-up", "register", "account", "subscribe", "unsubscribe",
    "newsletter", "cart", "checkout", "password", "logout",
    "cookie", "cookies", "gdpr", "ccpa", "data-privacy",
    # Compound legal/policy variants (exact-match can't catch these via "terms")
    "terms-of-use", "terms-of-service", "terms-and-conditions",
    "privacy-policy", "privacy-statement", "privacy-notice", "privacy-policies",
    "cookie-policy", "cookie-preferences", "cookie-settings",
    "code-of-conduct", "code-of-ethics",
    "return-policy", "refund-policy", "cancellation-policy",
    "anti-corruption", "anti-bribery", "anti-harassment",
    "community-guidelines",
    "data-processing-agreement", "service-level-agreement",
    "channel",  # /blog/channel/category  — always a category index, never an
                #   article, even when nested (e.g. /en/blog/channel/ai).
                #   Some sites use "channel" to prefix article slugs — those
                #   aren't real article pages either (they are category pages).
}

_SOFT_NON_ARTICLE: set[str] = {
    "about", "about-us", "contact", "contact-us", "careers", "career",
    "jobs", "policy", "policies", "pricing", "plans",
    "sitemap", "search", "tag", "tags", "category", "categories",
    "author", "authors", "rss", "feed", "feeds", "faq", "faqs", "support",
    "help", "press", "press-kit", "media-kit", "brand", "security",
    "status", "download", "downloads", "products", "product", "solutions",
    "company", "team", "partners", "investors",
    "trust", "compliance", "responsible-disclosure",
    "awards", "award", "achievement", "achievements",
    "leadership", "management", "board", "governance",
    "affiliates", "services", "service",
    "locations", "offices", "office",
    "patents", "trademarks", "licensing",
    "recognition", "testimonials", "clients",
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

    Rejection tiers (see denylist comments above):
      1. Asset file extension (*.pdf, *.jpg …).
      2. ANY segment in ``_HARD_NON_ARTICLE`` (legal/auth/account) — any depth.
      3. ANY segment in ``_SOFT_NON_ARTICLE`` (org sections) **only** when the
         path is shallow (≤2 segments); deeper URLs are presumed articles.
      4. Terminal segment in ``_NON_ARTICLE_TERMINAL`` (blog/news index) **and**
         the path is shallow (≤2 segments).
    """
    path = urlparse(absolute).path.lower()
    segments = [s for s in path.split("/") if s]
    if not segments:
        return True  # bare domain / homepage

    last = segments[-1]
    shallow = len(segments) <= 2  # noqa: PLR2004

    # ── Tier 1: asset files ─────────────────────────────────────────
    if "." in last:
        ext = last.rsplit(".", 1)[-1]
        if ext in _ASSET_EXTS:
            return True

    # ── Tier 2: hard denylist — reject at ANY depth ─────────────────
    # /privacy, /help/legal/terms-of-use, /x/y/privacy-policy …
    for seg in segments:
        if seg in _HARD_NON_ARTICLE:
            return True

    # ── Tier 3: soft denylist — reject only on shallow paths ────────
    # /about-us/team            → blocked (depth 2)
    # /company/blog/announce-x  → kept    (depth 3 — soft seg ignored)
    if shallow:
        for seg in segments:
            if seg in _SOFT_NON_ARTICLE:
                return True

    # ── Tier 4: terminal segment is a container / index page ────────
    # /quantum/blog               → blocked  (depth 2)
    # /quantum/blog/post-slug      → accepted (depth 3)
    if last in _NON_ARTICLE_TERMINAL and shallow:
        return True

    # ── Tier 4b: index pages (index.html, index.php, index.aspx) ────
    # /materials-science/news-events/events/index.php → blocked
    if last.startswith("index."):
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
            logger.debug("discover_links: skip non-article url %s", absolute)
            continue
        # Normalize: strip trailing slash so /path/ and /path are identical
        absolute = absolute.rstrip("/")
        if absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


def _render_with_playwright(url: str, timeout: float) -> str | None:
    """Render a JS-heavy page with Playwright if it's installed; else None.

    Uses ``wait_until="load"`` (not ``"networkidle"``) because modern sites
    with analytics/tracking scripts rarely reach network-idle within a
    reasonable timeout.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    def _do_launch(p, **kwargs):
        browser = p.chromium.launch(headless=True, **kwargs)
        page = browser.new_page(user_agent=_UA_STR)
        page.goto(url, wait_until="load",
                  timeout=int(timeout * 1000))
        # Extra wait for client-side rendering (Webflow, Next.js, etc.)
        page.wait_for_timeout(3000)
        html = page.content()
        browser.close()
        return html

    try:
        with sync_playwright() as p:
            return _do_launch(p)
    except Exception as exc:
        logger.warning(
            "Playwright default launch failed for %s: %s. "
            "Trying channel='chrome' …", url, exc)
        try:
            with sync_playwright() as p:
                return _do_launch(p, channel="chrome")
        except Exception as exc2:
            logger.warning(
                "Playwright channel='chrome' also failed for %s: %s",
                url, exc2)
            return None


def _fetch_html(url: str, render_js: bool = False,
                timeout: float = 20.0, retries: int = 3) -> str:
    """Fetch page HTML. With render_js, use Playwright (if available) for
    SPA/SSR sites, falling back to a plain httpx request.

    Transient failures (timeouts, connection errors, 5xx, 429) are retried up
    to ``retries`` times with exponential backoff. Permanent failures (4xx
    other than 429) are raised immediately without retrying.
    """
    if render_js:
        html = _render_with_playwright(url, timeout)
        if html:
            return html

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = httpx.get(url, timeout=timeout, follow_redirects=True,
                             headers=_UA)
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            last_exc = exc
            # 4xx (except 429) are permanent — don't waste retries on them.
            if code < 500 and code != 429:  # noqa: PLR2004
                raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
        if attempt < retries:
            backoff = 0.5 * (2 ** (attempt - 1))  # 0.5s, 1s, 2s, …
            logger.debug("fetch retry %d/%d for %s after %s (%.1fs backoff)",
                         attempt, retries, url, last_exc, backoff)
            time.sleep(backoff)
    assert last_exc is not None
    raise last_exc


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


def _discover_via_feeds_and_sitemap(
        base_url: str, page_html: str, include_pattern: str | None,
        exclude_pattern: str | None, timeout: float = 20.0) -> list[str]:
    """Fallback article discovery (②-2/②-3) when HTML link scraping found
    nothing: try the page's declared RSS/Atom feeds, then the site sitemap.

    RSS/Atom entries are curated lists of articles, so the ``_is_non_article``
    URL heuristic is *not* applied to them; sitemap URLs are broader, so it is.
    """
    found: list[str] = []
    seen: set[str] = set()

    def _accept(link: str, drop_non_article: bool) -> bool:
        if not link or link in seen:
            return False
        if include_pattern and include_pattern not in link:
            return False
        if exclude_pattern and exclude_pattern in link:
            return False
        if drop_non_article and _is_non_article(link):
            return False
        return True

    # 1. RSS / Atom autodiscovery from the list page.
    try:
        for feed_url in feed_discovery.find_feed_urls(page_html or "", base_url):
            try:
                xml = _fetch_html(feed_url, render_js=False, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                logger.warning("feed fetch failed %s: %s", feed_url, exc)
                continue
            for link in feed_discovery.feed_entry_links(xml):
                if _accept(link, drop_non_article=False):
                    seen.add(link)
                    found.append(link)
    except Exception as exc:  # noqa: BLE001
        logger.warning("rss discovery failed for %s: %s", base_url, exc)

    # 2. Sitemap (only if RSS yielded nothing).
    if not found:
        try:
            def _fetch(u: str) -> str:
                return _fetch_html(u, render_js=False, timeout=timeout)
            for link in feed_discovery.sitemap_urls(
                    base_url, _fetch, include_pattern=include_pattern):
                if _accept(link, drop_non_article=True):
                    seen.add(link)
                    found.append(link)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sitemap discovery failed for %s: %s", base_url, exc)

    if found:
        logger.info("fallback discovery (feed/sitemap) found %d links for %s",
                    len(found), base_url)
    return found


def _is_article_content(data: dict) -> bool:
    """Return False if extracted data looks like a list / landing / thin page
    rather than a real article.

    Checks:
      - Minimum content length (300 chars)
      - At least 3 non‑link text lines (i.e. not just a link index)
      - Title doesn't look like a navigation label or error page
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
    if len(text_lines) < 3:
        return False

    # ── Title quality: reject navigation/error/thin labels ──
    title = (data.get("title") or "").strip().lower()
    if not title or len(title) < 5:
        return False
    # Always-garbage titles (errors, 404 pages)
    if title in {"404", "page not found", "not found"}:
        return False
    # Navigation labels — only reject when the body is also thin (< 1000
    # chars).  A real article could genuinely be titled "Cloud
    # Infrastructure" or "Privacy"; a listing page never has a long body.
    _nav_labels = {
        "news", "events", "blog", "about", "contact", "home",
        "international", "careers", "press", "media", "resources",
        "products", "solutions", "services", "support", "faq",
        "privacy", "terms", "newsletter", "subscribe", "search",
        "archive", "category", "cloud infrastructure",
        "department news", "department events",
    }
    if title in _nav_labels and len(md) < 1000:  # noqa: PLR2004
        return False
    # Title containing only navigation-ish words (year-based listing pages)
    if re.match(r'^(news|events?|blog|press|media|about|contact)\s+\d{4}$', title):
        return False

    return True


def scrape_single(url: str, source_name: str, timeout: float = 20.0,
                  render_js: bool = True) -> Article | None:
    html = _fetch_html(url, render_js=render_js, timeout=timeout)
    data = extract_from_html(html, url=url)

    # Auto-fallback (②-1 / ①-4): if a non-rendered fetch produced no usable
    # article OR no publish date, retry once with JS rendering — recovers SPA
    # content and JS-injected dates (e.g. Next.js hydration).
    if not render_js and (not _is_article_content(data)
                          or not data.get("published_at")):
        try:
            html2 = _fetch_html(url, render_js=True, timeout=timeout)
        except Exception:  # noqa: BLE001
            html2 = ""
        if html2 and html2 != html:
            data2 = extract_from_html(html2, url=url)
            # prefer the rendered result if it is (now) a valid article
            if _is_article_content(data2):
                data = data2

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
                max_pages: int = 3, render_js: bool = True,
                timeout: float = 20.0) -> list[Article]:
    # Discover article links across the list page and (optionally) its
    # paginated siblings, then scrape each discovered article.
    article_links: list[str] = []
    seen_links: set[str] = set()
    visited_pages: set[str] = set()
    queue: list[str] = [url]
    source_normalized = url.rstrip("/")
    seed_html = ""
    pages = 0
    max_pages = max(1, max_pages)

    while queue and pages < max_pages:
        page_url = queue.pop(0)
        if page_url in visited_pages:
            continue
        visited_pages.add(page_url)
        pages += 1
        try:
            html = _fetch_html(page_url, render_js=render_js, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning("scrape_list: failed to fetch list page %s: %s",
                           page_url, exc)
            continue
        if page_url == url:
            seed_html = html
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

    # Fallback discovery: HTML link scraping found nothing (e.g. JS-rendered
    # list page that even rendering missed) — try RSS/Atom + sitemap.
    if not article_links:
        for link in _discover_via_feeds_and_sitemap(
                url, seed_html, include_pattern, exclude_pattern,
                timeout=timeout):
            if link in seen_links or link.rstrip("/") == source_normalized:
                continue
            seen_links.add(link)
            article_links.append(link)

    articles: list[Article] = []
    for link in article_links[:max_articles]:
        try:
            art = scrape_single(link, source_name, render_js=render_js,
                                timeout=timeout)
            if art:
                articles.append(art)
            else:
                logger.debug("scrape_list: %s yielded no article "
                             "(filtered or empty content)", link)
        except Exception as exc:  # noqa: BLE001
            logger.warning("scrape_list: failed to scrape %s: %s", link, exc)
            continue
        time.sleep(delay)
    return articles
