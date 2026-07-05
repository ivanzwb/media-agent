from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import trafilatura

from app.sources.date_parser import parse_date

# Hints that an <iframe> embeds a video (vs. ads / widgets).
_VIDEO_HINTS = (
    "youtube.com/embed", "youtube-nocookie.com", "youtu.be",
    "player.vimeo.com", "vimeo.com/video", "bilibili.com", "player.bilibili",
    "youku.com", "dailymotion.com/embed", "/embed/", "wistia",
    "brightcove", "ixigua.com", "v.qq.com",
)


_IMAGE_EXTS = ("png", "jpg", "jpeg", "webp", "gif", "avif")
# m3u8 = HLS playlist (streams .ts segments); downloaded via yt-dlp + ffmpeg.
_VIDEO_EXTS = ("mp4", "webm", "mov", "m4v", "ogv", "m3u8")
# Max images that get [[IMG:N]] position placeholders. Matches rewriter _MAX_IMG.
_MAX_IMG_PLACEHOLDERS = 12


def _attr(tag: str, name: str) -> str | None:
    m = re.search(name + r'\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))',
                  tag, re.I)
    if not m:
        return None
    return m.group(2) or m.group(3) or m.group(4)


def _media_urls_by_ext(html: str, exts: tuple[str, ...]) -> list[str]:
    """Quoted-string URLs ending in one of `exts` (catches media URLs embedded
    in <script> JSON, CSS background-image, srcset, etc.)."""
    pat = re.compile(
        r'["\']([^"\']*\.(?:' + "|".join(exts) + r')[^"\']*)["\']',
        re.I)
    return [m.group(1) for m in pat.finditer(html)]


def _images_with_placeholders(html: str, base_url: str | None = None
                              ) -> tuple[str, list[str]]:
    """Replace visible <img>/<picture>/<figure> with [[IMG:N]] text placeholders
    (in document order via a single left-to-right pass, up to
    _MAX_IMG_PLACEHOLDERS) so trafilatura keeps them in the markdown body at
    their original positions.

    Uses a combined regex to process all image-containing elements in document
    order (NOT grouped by element type), ensuring the returned image URL list
    matches the order images appear in the source HTML.

    Returns (modified_html, ordered_unique_image_urls).
    """
    imgs: list[str] = []
    index: dict[str, int] = {}

    def _idx(src: str) -> int:
        full = urljoin(base_url, src) if base_url else src
        if full not in index:
            if len(imgs) >= _MAX_IMG_PLACEHOLDERS:
                return -1  # past limit, keep original tag
            index[full] = len(imgs)
            imgs.append(full)
        return index[full]

    def _img_url(tag: str) -> str | None:
        u = (_attr(tag, "src") or _attr(tag, "data-src")
             or _attr(tag, "data-original"))
        if not u:
            ss = _attr(tag, "srcset") or _attr(tag, "data-srcset")
            if ss:
                u = ss.split(",")[0].strip().split(" ")[0]
        return u if (u and not u.startswith("data:")) else None

    # Combined left-to-right scan: matches <figure>, <picture>, or standalone
    # <img> in document order, whichever comes first at each position.
    # Named groups let the repl function know which type was matched.
    _IMG_PAT = re.compile(
        r'(?P<figure><figure[\s\S]*?</figure>)'
        r'|(?P<picture><picture[\s\S]*?</picture>)'
        r'|(?P<img><img\b[^>]*>)',
        re.I)

    def _repl(m):
        block = m.group(0)
        if m.lastgroup == 'figure':
            img_m = re.search(r'<img\b[^>]*>', block, re.I)
            if img_m:
                u = _img_url(img_m.group(0))
                if u:
                    idx = _idx(u)
                    if idx >= 0:
                        cap_m = re.search(
                            r'<figcaption[^>]*>(.*?)</figcaption>',
                            block, re.I | re.DOTALL)
                        if cap_m:
                            caption = re.sub(r'<[^>]+>', '',
                                             cap_m.group(1)).strip()
                            if caption:
                                return (f"<p>[[IMG:{idx}]]</p>\n"
                                        f"<p><em>{caption}</em></p>")
                        return f"<p>[[IMG:{idx}]]</p>"
            return block
        elif m.lastgroup == 'picture':
            img_m = re.search(r'<img\b[^>]*>', block, re.I)
            if img_m:
                u = _img_url(img_m.group(0))
                if u:
                    idx = _idx(u)
                    return (f"<p>[[IMG:{idx}]]</p>"
                            if idx >= 0 else block)
            ss_m = re.search(
                r'<source[^>]+srcset=["\']([^"\']+)["\']', block, re.I)
            if ss_m:
                u = ss_m.group(1).split(",")[0].strip().split(" ")[0]
                idx = _idx(u)
                return (f"<p>[[IMG:{idx}]]</p>"
                        if idx >= 0 else block)
            return block
        elif m.lastgroup == 'img':
            u = _img_url(block)
            if u:
                idx = _idx(u)
                if idx >= 0:
                    return f"<p>[[IMG:{idx}]]</p>"
            return block
        return block

    out = _IMG_PAT.sub(_repl, html)
    return out, imgs


def _images_from_html(html: str) -> list[str]:
    urls: list[str] = []
    for m in re.finditer(r'<img\b[^>]*>', html, re.I):
        tag = m.group(0)
        u = (_attr(tag, "src") or _attr(tag, "data-src")
             or _attr(tag, "data-original"))
        if not u:
            ss = _attr(tag, "srcset") or _attr(tag, "data-srcset")
            if ss:
                u = ss.split(",")[0].strip().split(" ")[0]
        if u:
            urls.append(u)
    for m in re.finditer(r'<source\b[^>]*>', html, re.I):
        ss = _attr(m.group(0), "srcset")
        if ss:
            urls.append(ss.split(",")[0].strip().split(" ")[0])
    # media URLs embedded in scripts / CSS / Next.js data
    urls.extend(_media_urls_by_ext(html, _IMAGE_EXTS))
    out: list[str] = []
    seen: set[str] = set()
    for u in urls:
        if u and not u.startswith("data:") and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _videos_from_html(html: str, base_url: str | None = None) -> list[str]:
    found: list[str] = []
    for m in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.I):
        src = m.group(1)
        if any(h in src.lower() for h in _VIDEO_HINTS):
            found.append(src)
    for m in re.finditer(
            r'<(?:video|source)[^>]+(?:src|data-src)=["\']([^"\']+)["\']',
            html, re.I):
        found.append(m.group(1))
    # video URLs filled in dynamically by JS (stored in <script> arrays, etc.)
    found.extend(_media_urls_by_ext(html, _VIDEO_EXTS))
    out: list[str] = []
    seen: set[str] = set()
    for u in found:
        full = urljoin(base_url, u) if base_url else u
        if full and full not in seen:
            seen.add(full)
            out.append(full)
    return out


def _videos_with_placeholders(html: str,
                              base_url: str | None = None) -> tuple[str, list]:
    """Replace each video tag with a `[[VIDEO:N]]` text placeholder (in place,
    document order) so trafilatura keeps it in the markdown body at the right
    spot. Returns (modified_html, ordered_video_urls)."""
    videos: list[str] = []
    index: dict[str, int] = {}

    def _idx(src: str) -> int:
        full = urljoin(base_url, src) if base_url else src
        if full not in index:
            index[full] = len(videos)
            videos.append(full)
        return index[full]

    def repl_iframe(m):
        src = m.group(1)
        if not any(h in src.lower() for h in _VIDEO_HINTS):
            return m.group(0)
        return f"<p>[[VIDEO:{_idx(src)}]]</p>"

    out = re.sub(r'<iframe[^>]+src=["\']([^"\']+)["\'][^>]*>(?:\s*</iframe>)?',
                 repl_iframe, html, flags=re.I)

    def repl_video(m):
        block = m.group(0)
        sm = re.search(r'(?:src|data-src)=["\']([^"\']+)["\']', block, re.I)
        if not sm:
            # JS-filled <video> with no static src — keep the tag (don't drop
            # it); the URL, if any, is captured from scripts below.
            return block
        return f"<p>[[VIDEO:{_idx(sm.group(1))}]]</p>"

    out = re.sub(r'<video[\s\S]*?</video>|<video[^>]*/?>', repl_video,
                 out, flags=re.I)

    # Append video URLs only present in scripts/JSON (no positioned tag) so
    # they still get archived/downloaded (rendered at the end as fallback).
    for u in _media_urls_by_ext(html, _VIDEO_EXTS):
        full = urljoin(base_url, u) if base_url else u
        if full and full not in index:
            index[full] = len(videos)
            videos.append(full)
    return out, videos


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def _title_from_html(html: str) -> str:
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL | re.IGNORECASE)
    if m:
        title = _strip_tags(m.group(1))
        if title:
            return title
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    if m:
        return _strip_tags(m.group(1))
    return ""


def _content_fallback(html: str) -> str:
    paras = re.findall(r"<p[^>]*>(.*?)</p>", html, re.DOTALL | re.IGNORECASE)
    texts = [_strip_tags(p) for p in paras]
    texts = [t for t in texts if t]
    return "\n\n".join(texts)


# An optionally backslash-escaped quote. Lets the JSON patterns below also
# match dates embedded in JS hydration blobs (Next.js RSC ``self.__next_f``,
# ``__NEXT_DATA__``, Nuxt, etc.) where the JSON is serialized inside a JS
# string and every quote is escaped as ``\"``.
_Q = r'\\?["\']'
# Date-ish value: digits + the punctuation found in ISO 8601 / common dates.
_DATEVAL = r'([0-9T:.+\-/ ]{6,40}Z?)'


def _meta_patterns(attr_value: str) -> list[str]:
    """Regexes for ``<meta property|name|itemprop="attr_value" content=...>``
    matching either attribute order (content before or after the key)."""
    return [
        rf'<meta\b[^>]*?(?:property|name|itemprop)=["\']{attr_value}["\']'
        rf'[^>]*?\bcontent=["\']([^"\']+)["\']',
        rf'<meta\b[^>]*?\bcontent=["\']([^"\']+)["\'][^>]*?'
        rf'(?:property|name|itemprop)=["\']{attr_value}["\']',
    ]


def _json_key_pattern(key: str) -> str:
    """Match ``"key":"<date>"`` inside JSON / JS blobs, tolerating escaped
    quotes (``\\"key\\":\\"...\\"``)."""
    return rf'{_Q}{key}{_Q}\s*:\s*{_Q}{_DATEVAL}'


def _build_date_patterns() -> list[str]:
    """Priority-ordered date patterns: trustworthy *published* signals first,
    then created/modified timestamps as a last resort."""
    pats: list[str] = []
    # 1. Strongest: explicit "published" meta (OpenGraph / article / og).
    for v in ("(?:og:|article:)?published_time", "article:published",
              "og:published_time"):
        pats += _meta_patterns(v)
    # 2. schema.org microdata <meta itemprop="datePublished">.
    pats += _meta_patterns("datePublished")
    # 3. JSON-LD / JS-hydration "published" keys (escaped-quote tolerant).
    #    NOTE: this is what recovers dates on Next.js/Sanity sites where the
    #    only date lives in the RSC stream as e.g. \"publishedOn\":\"...\".
    for k in ("datePublished", "publishedOn", "publishedAt", "publishDate",
              "published_time", "firstPublishedAt", "pubDate", "postDate",
              "publication_date"):
        pats.append(_json_key_pattern(k))
    # 4. Common CMS meta names.
    for v in ("pubdate", "publishdate", "date", r"dc\.date", r"dc\.date\.issued",
              "sailthru.date", "parsely-pub-date", "article.published"):
        pats += _meta_patterns(v)
    # 5. Visible text dates in the body (common CMS/blog patterns).
    #    These catch dates rendered in plain <div>/<p>/<span> text that
    #    aren't wrapped in <time> or meta tags — e.g. "Jun 16, 2026"
    #    or "4 May 2026".
    pats.append(r'(?<![\/\-\w])((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|'
                r'Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|'
                r'Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
                r'(?:\.)?\s+\d{1,2},?\s+\d{4})(?![\/\-\w])')
    pats.append(r'(?<![\/\-\w])(\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|'
                r'Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|'
                r'Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
                r'(?:\.)?(?:\s+\d{4}))(?![\/\-\w])')
    # 6. <time datetime="..."> — usually the publish date (sometimes a comment
    #    timestamp), so it sits below the explicit signals above.
    pats.append(r'<time\b[^>]*?\bdatetime=["\']([^"\']+)["\']')
    # 6. Last resort: created / modified timestamps.
    for k in ("dateCreated", "dateModified", "_createdAt", "_updatedAt",
              "createdAt", "modified_time"):
        pats.append(_json_key_pattern(k))
    for v in ("article:modified_time", "og:updated_time"):
        pats += _meta_patterns(v)
    return pats


_DATE_PATTERNS: list[str] = _build_date_patterns()


def _date_from_html(html: str) -> datetime | None:
    """Extract a publish (or, failing that, created/modified) date.

    Scans, in priority order: OpenGraph/article meta, schema.org microdata,
    JSON-LD and JS-hydration blobs (Next.js RSC / ``__NEXT_DATA__`` — including
    backslash-escaped JSON), common CMS meta names, ``<time>`` elements, and
    finally created/modified timestamps. Returns the first value that parses.
    """
    for pat in _DATE_PATTERNS:
        for m in re.finditer(pat, html, re.I):
            parsed = parse_date(m.group(1))
            if parsed is not None:
                return parsed
    return None


def _fix_markdown_tables(md: str) -> str:
    """Compact markdown tables by removing blank lines between rows.

    trafilatura inserts blank lines between consecutive table rows, which
    causes each row to render as a separate, broken one-row table.  This
    function merges them back into a single contiguous table block.
    """
    lines = md.split("\n")
    out: list[str] = []
    buffered_rows: list[str] = []   # pending table rows waiting to be flushed
    in_table = False

    def _is_table_row(s: str) -> bool:
        """A line containing | is a table row (trafilatura omits leading pipes,
        e.g. `Col1 | Col2` instead of `| Col1 | Col2 |`)."""
        return "|" in s

    def _is_separator(s: str) -> bool:
        """Header/body separator: `|---|----|---|`"""
        return _is_table_row(s) and bool(
            __import__("re").match(r"^\|[\s\-:|]+\|$", s))

    def _flush_table():
        nonlocal in_table
        if not buffered_rows:
            return
        # Ensure a blank line before the table block
        if out and out[-1] != "":
            out.append("")
        out.extend(buffered_rows)
        out.append("")          # blank line after table
        buffered_rows.clear()
        in_table = False

    for line in lines:
        if _is_table_row(line):
            if not in_table:
                in_table = True
            buffered_rows.append(line)
        else:
            if in_table:
                if line == "":
                    # Blank line inside a table — skip it to keep rows tight.
                    continue
                # Non-table, non-blank line → table has ended.
                _flush_table()
            out.append(line)

    # Flush any trailing table
    _flush_table()

    return "\n".join(out)


# ── JSON-LD image extraction ──────────────────────────────────────────────

_JSONLD_IMAGE_RE = re.compile(
    r'"image"\s*:\s*(?:\{[^}]*"url"\s*:\s*"([^"]+)"[^}]*\}|"([^"]+)")',
    re.I | re.DOTALL)

_NON_ARTICLE_PATTERNS = [
    # Loading spinners / placeholders
    re.compile(r'(?:page_loader|spinner|loading|placeholder)\.(?:png|gif|svg)', re.I),
    # CMS thumbnail resize directives (SilverStripe _FillWz, Craft _transform, etc.)
    re.compile(r'(?:_FillWz|_transform|__Scale|__ResizedImage)', re.I),
    # Site favicons / touch icons (favicon, favicon-32x32, apple-touch-icon, etc.)
    re.compile(r'(?:favicon|touchicon|apple[-.]touch|og_logo)[\w-]*\.(?:png|ico|jpg|svg)', re.I),
]


def _jsonld_image(html: str, base_url: str | None = None) -> str | None:
    """Extract the primary ``image`` URL from JSON-LD structured data
    (Schema.org Article / NewsArticle)."""
    # Find JSON-LD blocks: <script type="application/ld+json">…</script>
    for block in re.finditer(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
        html, re.I | re.DOTALL,
    ):
        json_text = block.group(1).strip()
        if '"@type"' not in json_text:
            continue
        m = _JSONLD_IMAGE_RE.search(json_text)
        if m:
            # Group 1 = nested url, Group 2 = flat string
            img = m.group(1) or m.group(2)
            if img and base_url:
                img = urljoin(base_url, img)
            return img if img else None
    return None


def _filter_article_images(images: list[str]) -> list[str]:
    """Remove images that are clearly site chrome (loaders, thumbnails,
    favicons) rather than article content."""
    out: list[str] = []
    for u in images:
        if any(p.search(u) for p in _NON_ARTICLE_PATTERNS):
            continue
        out.append(u)
    return out


# ── Cookie consent / privacy boilerplate patterns ────────────────────
# These match cookie CONSENT NOTICES (overlay/popup templates), NOT
# legitimate articles that happen to discuss cookies.
# Key distinction: consent notices use formulaic legal/corporate phrasing
# ("We use cookies to analyze...", "Cookie Settings", "browsing experience")
# while articles about cookies have varied educational/editorial language.
_COOKIE_CONSENT_PATTERNS = (
    # "We use cookies" / "This website uses cookies" / "Our site uses cookies"
    re.compile(r'(we|this\s+site|this\s+website|our\s+site)\s+uses?\s+cookies?\s', re.I),
    # "Cookie Settings" / "Cookie Preferences" / "Cookie Policy" / "Cookie Notice"
    re.compile(r'cookie\s+(settings|preferences|notice|policy|consent)', re.I),
    # "Accept/Reject/Allow/Decline all cookies"
    re.compile(r'(accept|reject|allow|decline)\s+(all\s+)?cookies?\b', re.I),
    # "Manage your cookie/consent/preferences"
    re.compile(r'manage your (cookie|cookies|consent|preferences)', re.I),
    # "Customize your cookie settings"
    re.compile(r'customize your cookie', re.I),
    # "browsing experience"  (very specific to consent overlays)
    re.compile(r'browsing experience', re.I),
    # "analytics partners/providers/services"
    re.compile(r'analytics (partners?|providers?|services?)', re.I),
    # "Share information with our analytics/partners"
    re.compile(r'share\s+(information|data)\s+with\s+our', re.I),
    # "By continuing to use/browse/visit"
    re.compile(r'by continuing to (use|browse|visit)', re.I),
    # "To improve/enhance your browsing/user experience"
    re.compile(r'(improve|enhance|analyze).*(browsing|user)\s+experience', re.I),
    # "Learn more about our cookie/privacy policy"
    re.compile(r'learn more about our (cookie|privacy)', re.I),
    # Cookie consent vendor markers (OneTrust, Cookiebot, etc.)
    re.compile(r'(onetrust|optanonwrapper|cookiebot)', re.I),
)


def _is_cookie_consent_line(text: str) -> bool:
    """True if *text* matches known cookie consent boilerplate (not an article
    about cookies).  Uses specific consent-template patterns rather than a
    blunt keyword check so legitimate articles are never stripped."""
    return any(p.search(text) for p in _COOKIE_CONSENT_PATTERNS)


def _strip_cookie_consent(md: str) -> str:
    """If ALL non-placeholder text lines match cookie-consent boilerplate
    patterns, strip the consent text and return only image/video placeholders.
    Mixed content or genuine articles about cookies are left untouched.

    This prevents cookie-overlay-only pages from being archived as articles
    while preserving legitimate articles that discuss cookies or privacy."""
    lines = md.split("\n")
    real = [
        l.strip() for l in lines
        if l.strip()
        and not l.strip().startswith("[[IMG:")
        and not l.strip().startswith("[[VIDEO:")
    ]
    if not real:
        return md
    if not all(_is_cookie_consent_line(l) for l in real):
        return md  # mixed or genuine article — keep as-is
    # All real text is consent boilerplate — keep only placeholders
    kept = [l for l in lines
            if l.strip().startswith("[[IMG:")
            or l.strip().startswith("[[VIDEO:") or not l.strip()]
    return "\n".join(kept).strip() if kept else ""


def extract_from_html(html: str, url: str) -> dict:
    content_md = ""
    images: list[str] = []
    videos: list[str] = []

    # ── 1. readability-lxml: extract main article HTML (removes nav/sidebar) ──
    try:
        from readability import Document
        readable = Document(html)
        article_html = readable.summary()
        if article_html:
            # Insert [[IMG:N]] / [[VIDEO:N]] placeholders in the EXTRACTED
            # article HTML only, so images outside the article body (header
            # logos, sidebar ads, etc.) are simply ignored.
            article_pl, images = _images_with_placeholders(
                article_html, base_url=url)
            article_pl, videos = _videos_with_placeholders(
                article_pl, base_url=url)
            content_md = trafilatura.extract(
                article_pl, output_format="markdown",
                include_images=True, include_links=True, url=url) or ""
    except Exception:                          # noqa: BLE001
        pass

    # ── 1b. readability got body text but no images — fallback to full HTML ──
    # Some sites (PageCloud, Webflow, SilverStripe) embed images in ways
    # readability strips, leaving body text intact but images empty.
    # Re-extract images from the untouched HTML, but filter out site chrome
    # (loading spinners, related-article thumbnails, favicons).
    if content_md.strip() and not images:
        html_pl, images = _images_with_placeholders(html, base_url=url)
        # Also extract the primary image from JSON-LD structured data
        # (Schema.org Article/NewsArticle) — many sites store the hero image
        # only in JSON-LD, not as a visible <img> tag.
        jsonld_img = _jsonld_image(html, base_url=url)
        if jsonld_img and jsonld_img not in images:
            # Avoid duplicates when the same image appears under different
            # domains (e.g. www.site.com vs site.com after redirect).
            jsonld_slug = jsonld_img.rsplit("/", 1)[-1].split("?")[0]
            if not any(jsonld_slug in u for u in images):
                images.insert(0, jsonld_img)

    # ── 2. trafilatura directly on original HTML (fallback) ──
    if not content_md.strip():
        html_pl, images = _images_with_placeholders(html, base_url=url)
        html_pl, videos = _videos_with_placeholders(html_pl, base_url=url)
        content_md = trafilatura.extract(
            html_pl, output_format="markdown", include_images=True,
            include_links=True, url=url) or ""

    # ── 3. plain <p> tag extraction (last resort) ──
    if not content_md.strip():
        content_md = _content_fallback(html)
        images = [urljoin(url, u) for u in _images_from_html(html)]
        videos = [urljoin(url, u) for u in _videos_from_html(html)]

    # ── 3b. Strip site chrome (loaders, favicons, CMS thumbnails) ──
    # Applied to ALL extraction paths so sidebar/header cruft that leaked
    # through steps 2-3 is removed in addition to the step-1b filtering.
    images = _filter_article_images(images)

    # ── 4. Re-inject orphaned image placeholders ──
    # If trafilatura stripped some [[IMG:N]] placeholders (e.g. figure
    # elements readability kept but trafilatura dropped), prepend them
    # at the very top so they aren't lost entirely.
    orphans: list[str] = []
    for i in range(len(images)):
        placeholder = f"[[IMG:{i}]]"
        if placeholder not in content_md:
            orphans.append(placeholder)
    if orphans:
        content_md = "\n\n".join(orphans) + "\n\n" + content_md

    content_md = _fix_markdown_tables(content_md)

    # ── 5. Strip cookie consent / privacy boilerplate ──────────────────
    # Cookie overlays that readability/trafilatura extract as "article body"
    # are not real content.  Stripping them here leaves only placeholders,
    # which naturally fail the article quality check in scrape_single.
    content_md = _strip_cookie_consent(content_md)

    images = [urljoin(url, u) for u in images]

    # ── Sanity checks ──────────────────────────────────────────────────
    dt = _date_from_html(html)
    now = datetime.now(timezone.utc)
    # Reject dates more than 1 day in the future (common when scraping
    # listing/event pages that have upcoming dates listed).
    if dt is not None and dt > now + timedelta(days=1):
        dt = None

    return {
        "title": _title_from_html(html),
        "content_md": content_md,
        "images": images,
        "videos": videos,
        "published_at": dt,
    }
