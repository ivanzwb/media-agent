from __future__ import annotations

import re
from urllib.parse import urljoin

import trafilatura

# Hints that an <iframe> embeds a video (vs. ads / widgets).
_VIDEO_HINTS = (
    "youtube.com/embed", "youtube-nocookie.com", "youtu.be",
    "player.vimeo.com", "vimeo.com/video", "bilibili.com", "player.bilibili",
    "youku.com", "dailymotion.com/embed", "/embed/", "wistia",
    "brightcove", "ixigua.com", "v.qq.com",
)


_IMAGE_EXTS = ("png", "jpg", "jpeg", "webp", "gif", "avif")
_VIDEO_EXTS = ("mp4", "webm", "mov", "m4v", "ogv")


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
        r'["\']([^"\']+?\.(?:' + "|".join(exts) + r'))(?:\?[^"\']*)?["\']',
        re.I)
    return [m.group(1) for m in pat.finditer(html)]


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


def extract_from_html(html: str, url: str) -> dict:
    # Insert [[VIDEO:N]] placeholders where videos appear, so they stay in the
    # body at their original position instead of being dumped at the end.
    html_pl, videos = _videos_with_placeholders(html, base_url=url)
    content_md = trafilatura.extract(
        html_pl, output_format="markdown", include_images=True,
        include_links=True, url=url) or ""
    if not content_md.strip():
        content_md = _content_fallback(html)
    images = [urljoin(url, u) for u in _images_from_html(html)]
    return {
        "title": _title_from_html(html),
        "content_md": content_md,
        "images": images,
        "videos": videos,
    }
