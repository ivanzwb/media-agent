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


def _images_from_html(html: str) -> list[str]:
    return re.findall(r'<img[^>]+src="([^"]+)"', html)


def _videos_from_html(html: str, base_url: str | None = None) -> list[str]:
    found: list[str] = []
    for m in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.I):
        src = m.group(1)
        if any(h in src.lower() for h in _VIDEO_HINTS):
            found.append(src)
    for m in re.finditer(r'<(?:video|source)[^>]+src=["\']([^"\']+)["\']',
                         html, re.I):
        found.append(m.group(1))
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
        sm = re.search(r'src=["\']([^"\']+)["\']', block, re.I)
        if not sm:
            return ""
        return f"<p>[[VIDEO:{_idx(sm.group(1))}]]</p>"

    out = re.sub(r'<video[\s\S]*?</video>|<video[^>]*/?>', repl_video,
                 out, flags=re.I)
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
