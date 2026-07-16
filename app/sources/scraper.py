from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
from trafilatura.settings import use_config
from trafilatura.spider import focused_crawler

from app.models import Article
from app.sources import feed_discovery
from app.sources.extractor import extract_from_html

logger = logging.getLogger(__name__)

# How many pages focused_crawler fetches per iterative call (small batches let
# us re-check the overall time budget between calls).
_CRAWL_BATCH = 5

_UA_STR = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_UA = {"User-Agent": _UA_STR}

# Rotated User-Agents — cycle through on retry to evade Cloudflare/WAF blocks.
_ROTATED_UAS = [
    _UA_STR,
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 Edg/124.0",
]

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
    # Also handles /newsletter.html, /cookie-policy.aspx etc. by stripping
    # common server-side extensions from the segment before matching.
    for seg in segments:
        if seg in _HARD_NON_ARTICLE:
            return True
        # Strip .html/.htm/.php/.aspx suffix for hard-denylist matching
        seg_stripped = re.sub(r'\.(html?|php|aspx?|jsp)$', '', seg)
        if seg_stripped != seg and seg_stripped in _HARD_NON_ARTICLE:
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


def _accept_link(absolute: str, base_host: str,
                 include_pattern: str | None,
                 exclude_pattern: str | None) -> bool:
    """Shared accept gate for a candidate article URL.

    A URL is accepted when it is same-host, matches the (optional) include
    substring, does NOT match the (optional) exclude substring, and is not
    rejected by the ``_is_non_article`` URL heuristic.  Used by both the
    1-level ``discover_links`` scraper and the deep ``crawl_site_links``
    crawler so both apply identical (unloosened) filters.
    """
    if urlparse(absolute).netloc != base_host:
        return False
    if include_pattern and include_pattern not in absolute:
        return False
    if exclude_pattern and exclude_pattern in absolute:
        return False
    if _is_non_article(absolute):
        return False
    return True


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
        if not _accept_link(absolute, base_host, include_pattern,
                            exclude_pattern):
            logger.debug("discover_links: skip url %s", absolute)
            continue
        # Normalize: strip trailing slash so /path/ and /path are identical
        absolute = absolute.rstrip("/")
        if absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


def crawl_site_links(base_url: str, *,
                     include_pattern: str | None = None,
                     exclude_pattern: str | None = None,
                     max_pages: int = 3, delay: float = 1.0,
                     timeout: float = 20.0,
                     proxy: str | None = None) -> list[str]:
    """Deep-crawl an entire site starting from ``base_url`` and return a
    deduped, normalized list of probable article URLs (multi-level).

    Uses trafilatura's ``focused_crawler`` (breadth-first, same-host, static
    fetch) iteratively — feeding ``todo``/``known_links`` back into each call
    — until the crawl frontier is empty, the page-fetch budget is spent, or an
    overall time budget elapses.  The accumulated ``known_links`` are then
    filtered down to article candidates with the SAME gates as
    ``discover_links`` (``include_pattern``/``exclude_pattern`` +
    ``_is_non_article``); the filters are NOT loosened here.

    Budgets are derived from ``max_pages`` (which historically meant the number
    of pagination pages to follow, and now caps how many pages the crawler
    FETCHES for link discovery):

      * page budget   = ``max(max_pages * 10, 30)`` pages fetched
      * known-URL cap = ``max(page_budget * 50, 2000)`` on-site URLs
      * time budget   = ``page_budget * (delay + 3)`` s, clamped to [45, 240]

    Politeness: ``delay`` is passed to the crawler as ``SLEEP_TIME`` (honoured
    between fetches, unless robots.txt specifies a larger crawl-delay).

    PROXY LIMITATION: trafilatura's downloader only honours a proxy via the
    ``http_proxy`` environment variable (SOCKS proxy manager), read once at
    import time — it cannot use our per-request ``proxy`` argument.  We
    therefore do NOT apply ``proxy`` to the crawl phase (only log it).  If a
    site is reachable solely through the proxy, the crawler simply finds
    nothing and ``scrape_list`` falls back to the proxy-aware 1-level /
    RSS / sitemap discovery paths.  Per-article extraction (``scrape_single``
    → ``_fetch_html``) still uses ``proxy`` normally.
    """
    if proxy:
        logger.debug("crawl_site_links: proxy %s is not applied to the "
                     "focused crawler (trafilatura limitation); per-article "
                     "extraction and fallbacks still use it.", proxy)

    crawl_start = base_url.rstrip("/") or base_url
    base_host = urlparse(crawl_start).netloc
    source_norm = crawl_start

    max_pages = max(1, max_pages)
    page_budget = max(max_pages * 10, 30)
    known_cap = max(page_budget * 50, 2000)
    time_budget = min(max(page_budget * (max(delay, 0.0) + 3.0), 45.0), 240.0)

    cfg = use_config()
    cfg.set("DEFAULT", "SLEEP_TIME", str(max(delay, 0.0)))
    cfg.set("DEFAULT", "DOWNLOAD_TIMEOUT", str(max(1, int(timeout))))
    cfg.set("DEFAULT", "USER_AGENTS", _UA_STR)

    todo: list[str] | None = None
    known: list[str] | None = None
    seen_pages = 0
    deadline = time.monotonic() + time_budget

    try:
        while seen_pages < page_budget and time.monotonic() < deadline:
            batch = min(_CRAWL_BATCH, page_budget - seen_pages)
            todo, known = focused_crawler(
                crawl_start, max_seen_urls=batch, max_known_urls=known_cap,
                todo=todo, known_links=known, config=cfg)
            seen_pages += batch
            if not todo:
                break  # frontier exhausted — whole site crawled
    except Exception as exc:  # noqa: BLE001
        logger.warning("crawl_site_links: focused_crawler failed for %s: %s",
                       crawl_start, exc)

    known = known or []
    out: list[str] = []
    seen_urls: set[str] = set()
    for link in known:
        if not link:
            continue
        if not _accept_link(link, base_host, include_pattern, exclude_pattern):
            continue
        norm = link.rstrip("/")
        if norm == source_norm or norm in seen_urls:
            continue
        seen_urls.add(norm)
        out.append(norm)

    logger.info("crawl_site_links: %s → %d known, %d article candidates "
                "(fetched ~%d pages)", crawl_start, len(known), len(out),
                seen_pages)
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
        response = page.goto(url, wait_until="load",
                             timeout=int(timeout * 1000))
        # Reject HTTP error pages (4xx, 5xx) — Playwright renders them as
        # fully-styled HTML which would pass through as fake "article" content.
        if response and response.status >= 400:
            logger.debug("playwright: %s returned HTTP %d — skipping",
                         url, response.status)
            browser.close()
            return None
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


def _client_get(url: str, timeout: float, headers: dict[str, str],
                proxy: str | None = None) -> httpx.Response:
    """Fetch a URL via HTTP GET with timeout, follow-redirects, and optional proxy.

    When *proxy* is set (e.g. ``http://127.0.0.1:7890``), all HTTP calls go
    through that proxy — useful for bypassing Cloudflare/WAF or GFW blocks.

    When *proxy* is ``None``, falls back to ``httpx.get()`` for compatibility
    with downstream tests that patch ``httpx.get`` directly.
    """
    if proxy:
        with httpx.Client(proxy=proxy, timeout=timeout,
                          follow_redirects=True) as client:
            return client.get(url, headers=headers)
    return httpx.get(url, timeout=timeout, follow_redirects=True,
                     headers=headers)


def _fetch_html(url: str, render_js: bool = False,
                timeout: float = 20.0, retries: int = 3,
                proxy: str | None = None) -> str:
    """Fetch page HTML. With render_js, use Playwright (if available) for
    SPA/SSR sites, falling back to a plain httpx request.

    Transient failures (timeouts, connection errors, 5xx, 429) are retried up
    to ``retries`` times with exponential backoff. Permanent failures (4xx
    other than 429) are raised immediately without retrying.

    If *proxy* is set (e.g. ``http://127.0.0.1:7890``), all HTTP calls go
    through that proxy — useful for bypassing Cloudflare/WAF or GFW blocks.
    """
    if render_js:
        html = _render_with_playwright(url, timeout)
        if html:
            # JS rendering can strip code blocks or alter article text.
            # Always also fetch the plain HTML and keep whichever yields
            # more content after extraction.
            try:
                resp = _client_get(url, timeout, _UA, proxy=proxy)
                resp.raise_for_status()
                plain = resp.text
            except Exception:
                plain = ""
            if plain:
                rendered_data = extract_from_html(html, url=url)
                plain_data = extract_from_html(plain, url=url)
                rendered_len = len(rendered_data.get("content_md", "") or "")
                plain_len = len(plain_data.get("content_md", "") or "")
                if plain_len > rendered_len * 1.2:
                    # Plain fetch is meaningfully richer — use it instead.
                    logger.info("scraper: plain fetch richer for %s "
                                "(%d vs %d chars)", url, plain_len, rendered_len)
                    return plain
            return html

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            ua = _ROTATED_UAS[(attempt - 1) % len(_ROTATED_UAS)]
            resp = _client_get(url, timeout, {"User-Agent": ua}, proxy=proxy)
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
        exclude_pattern: str | None, timeout: float = 20.0,
        proxy: str | None = None) -> list[str]:
    """Fallback article discovery (②-2/②-3) when HTML link scraping found
    nothing: try the page's declared RSS/Atom feeds, then the site sitemap.

    RSS/Atom entries are curated lists of articles, so the ``_is_non_article``
    URL heuristic is *not* applied to them; sitemap URLs are broader, so it is.

    If *proxy* is set (e.g. ``http://127.0.0.1:7890``), all HTTP calls go
    through that proxy — useful for bypassing Cloudflare/WAF or GFW blocks.
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
                xml = _fetch_html(feed_url, render_js=False, timeout=timeout,
                                  proxy=proxy)
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
                return _fetch_html(u, render_js=False, timeout=timeout,
                                   proxy=proxy)
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
        if l.strip()
        and not l.strip().startswith("http")
        and not l.startswith("![")
        and not l.strip().startswith("[[IMG:")
        and not l.strip().startswith("[[VIDEO:")
    ]
    if len(text_lines) < 3:
        return False

    # ── Link-density check: listing/year-index pages are mostly links ──
    # Real articles have prose; listing pages have many [text](url) links.
    # If >40% of non-whitespace characters are inside link markups, reject.
    link_chars = sum(len(m.group(0)) for m in
                     re.finditer(r'\[([^\]]+)\]\([^)]+\)', md))
    total_chars = len(re.sub(r'\s', '', md))
    if total_chars > 0 and link_chars / total_chars > 0.4:
        return False

    # ── Always reject error/404 pages ──
    title = (data.get("title") or "").strip().lower()
    # Exact match first
    if title in {"404", "page not found", "not found"}:
        return False
    # Broader match: title contains "404" AND "not found" (e.g. "404 Not Found – Emotiv")
    if "404" in title and "not found" in title:
        return False

    # ── Reject listing/archive/newsletter pages ──
    # These are not individual articles even when they pass length checks.
    listing_keywords = re.compile(
        r'^(news(\s*&?\s*events)?|newsletter|archive|blog\s+archive'
        r'|events?\s+archive|article\s+archive|press\s+release)',
        re.I,
    )
    if listing_keywords.match(title.replace("\u00a0", " ")):
        return False

    return True


def scrape_single(url: str, source_name: str, timeout: float = 20.0,
                  render_js: bool = True,
                  proxy: str | None = None) -> Article | None:
    html = _fetch_html(url, render_js=render_js, timeout=timeout, proxy=proxy)
    data = extract_from_html(html, url=url)

    # Auto-fallback (②-1 / ①-4): if a non-rendered fetch produced no usable
    # article OR no publish date, retry once with JS rendering — recovers SPA
    # content and JS-injected dates (e.g. Next.js hydration).
    if not render_js and (not _is_article_content(data)
                          or not data.get("published_at")):
        try:
            html2 = _fetch_html(url, render_js=True, timeout=timeout,
                                proxy=proxy)
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


def _discover_one_level(url: str, include_pattern: str | None,
                        exclude_pattern: str | None, max_pages: int,
                        render_js: bool, timeout: float,
                        proxy: str | None) -> tuple[list[str], str]:
    """1-level article discovery: fetch the list page (+ its ``?page=N`` /
    ``/page/N`` paginated siblings, up to ``max_pages``) via ``_fetch_html``
    and scrape same-host article links from each.

    This is the pre-deep-crawl behaviour, retained as an SPA fallback: the
    deep crawler (``focused_crawler``) is static-fetch only, so a JS-rendered
    list page can yield nothing there but still resolve here because
    ``_fetch_html(render_js=True)`` renders it with Playwright.

    Returns ``(article_links, seed_html)`` where ``seed_html`` is the raw HTML
    of the landing page (used by the RSS/sitemap fallback for feed autodiscovery).
    """
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
            html = _fetch_html(page_url, render_js=render_js, timeout=timeout,
                               proxy=proxy)
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
    return article_links, seed_html


def scrape_list(url: str, source_name: str, include_pattern: str | None = None,
                exclude_pattern: str | None = None,
                max_articles: int = 10, delay: float = 1.0,
                max_pages: int = 3, render_js: bool = True,
                timeout: float = 20.0,
                proxy: str | None = None) -> list[Article]:
    """Deep-crawl the site at *url* to discover article pages across multiple
    levels, then extract each candidate into an :class:`Article`.

    Discovery order (first non-empty result wins):
      ① Deep crawl — ``crawl_site_links`` drives trafilatura's
         ``focused_crawler`` breadth-first over the whole host (multi-level),
         bounded by budgets derived from ``max_pages``.
      ② SPA fallback — ``_discover_one_level`` (Playwright-rendered landing
         page + pagination), for JS-only sites the static crawler can't read.
      ③ Feed/sitemap fallback — ``_discover_via_feeds_and_sitemap``.

    Each candidate is then fetched via ``scrape_single`` (which keeps the
    Playwright JS-render fallback + ``_is_article_content`` quality gate),
    capped at ``max_articles`` and separated by ``delay`` seconds.
    """
    source_normalized = url.rstrip("/")
    seed_html = ""

    # ① Deep, multi-level crawl of the whole site.
    try:
        article_links = crawl_site_links(
            url, include_pattern=include_pattern,
            exclude_pattern=exclude_pattern, max_pages=max_pages,
            delay=delay, timeout=timeout, proxy=proxy)
    except Exception as exc:  # noqa: BLE001
        logger.warning("scrape_list: deep crawl failed for %s: %s", url, exc)
        article_links = []

    # ② SPA fallback: static crawler found nothing (JS-rendered list page).
    if not article_links:
        article_links, seed_html = _discover_one_level(
            url, include_pattern, exclude_pattern, max_pages, render_js,
            timeout, proxy)

    # ③ RSS/Atom + sitemap fallback: link discovery found nothing at all.
    if not article_links:
        for link in _discover_via_feeds_and_sitemap(
                url, seed_html, include_pattern, exclude_pattern,
                timeout=timeout, proxy=proxy):
            article_links.append(link)

    # Dedupe (normalized) while preserving discovery order.
    deduped: list[str] = []
    seen_links: set[str] = set()
    for link in article_links:
        norm = link.rstrip("/")
        if norm == source_normalized or norm in seen_links:
            continue
        seen_links.add(norm)
        deduped.append(link)
    article_links = deduped

    articles: list[Article] = []
    for link in article_links[:max_articles]:
        try:
            art = scrape_single(link, source_name, render_js=render_js,
                                timeout=timeout, proxy=proxy)
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
