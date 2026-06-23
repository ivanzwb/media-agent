from __future__ import annotations

import re

import trafilatura


def _images_from_html(html: str) -> list[str]:
    return re.findall(r'<img[^>]+src="([^"]+)"', html)


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
    content_md = trafilatura.extract(
        html, output_format="markdown", include_images=True,
        include_links=True, url=url) or ""
    if not content_md.strip():
        content_md = _content_fallback(html)
    return {
        "title": _title_from_html(html),
        "content_md": content_md,
        "images": _images_from_html(html),
    }
