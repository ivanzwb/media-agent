"""Minimal Markdown -> WeChat-friendly HTML.

WeChat 图文 ``content`` is an HTML fragment. We only need the subset the
rewriter emits: headings, paragraphs, bold/italic/code/links, images, lists,
blockquotes, hr. Video placeholders ([[VIDEO:N]], iframes, [▶ ...]) are
dropped from 图文 (video is handled separately).
"""
from __future__ import annotations

import html as _html
import re

_IMG_RE = re.compile(r'!\[(?P<alt>[^\]]*)\]\((?P<url>[^)\s]+)[^)]*\)')
_LINK_RE = re.compile(r'\[(?P<text>[^\]]+)\]\((?P<url>[^)\s]+)[^)]*\)')
_BOLD_RE = re.compile(r'\*\*(?P<t>.+?)\*\*')
_ITALIC_RE = re.compile(r'(?<!\*)\*(?P<t>[^*]+?)\*(?!\*)')
_CODE_RE = re.compile(r'`(?P<t>[^`]+?)`')
_VIDEO_PLACEHOLDER_RE = re.compile(r'\[\[VIDEO:\d+\]\]')

_IMG_SRC_RE = re.compile(r'<img\b[^>]*\bsrc="([^"]+)"')


def _inline(text: str) -> str:
    """Apply inline markdown to an already-structured text line."""
    # Images first (before links, same bracket syntax).
    def img_sub(m):
        alt = _html.escape(m.group("alt"))
        url = _html.escape(m.group("url"), quote=True)
        return f'<img src="{url}" alt="{alt}" style="max-width:100%;"/>'
    text = _IMG_RE.sub(img_sub, text)

    # Escape any remaining raw HTML-significant chars in the plain text, but
    # we've already injected <img> tags — protect them with placeholders.
    tags: list[str] = []

    def stash(m):
        tags.append(m.group(0))
        return f"\x00{len(tags) - 1}\x00"
    text = re.sub(r'<img[^>]*/>', stash, text)
    text = _html.escape(text)

    def link_sub(m):
        label = m.group("text")
        url = _html.escape(m.group("url"), quote=True)
        return f'<a href="{url}">{label}</a>'
    text = _LINK_RE.sub(link_sub, text)
    text = _BOLD_RE.sub(lambda m: f"<strong>{m.group('t')}</strong>", text)
    text = _ITALIC_RE.sub(lambda m: f"<em>{m.group('t')}</em>", text)
    text = _CODE_RE.sub(lambda m: f"<code>{m.group('t')}</code>", text)

    # restore stashed <img> tags
    text = re.sub(r'\x00(\d+)\x00', lambda m: tags[int(m.group(1))], text)
    return text


def markdown_to_html(md: str) -> str:
    md = _VIDEO_PLACEHOLDER_RE.sub("", md or "")
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    list_type: str | None = None  # 'ul' | 'ol'

    def flush_para():
        if para:
            out.append("<p>" + "<br/>".join(_inline(x) for x in para) + "</p>")
            para.clear()

    def flush_list():
        nonlocal list_type
        if list_type:
            out.append(f"</{list_type}>")
            list_type = None

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            flush_para()
            flush_list()
            continue

        # skip leftover video embeds
        if stripped.startswith("<iframe") or stripped.startswith("[▶"):
            flush_para()
            flush_list()
            continue

        m = re.match(r'^(#{1,6})\s+(.*)$', stripped)
        if m:
            flush_para()
            flush_list()
            level = min(len(m.group(1)), 4)
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            continue

        if re.match(r'^(-{3,}|\*{3,}|_{3,})$', stripped):
            flush_para()
            flush_list()
            out.append("<hr/>")
            continue

        if stripped.startswith(">"):
            flush_para()
            flush_list()
            out.append(f"<blockquote>{_inline(stripped[1:].strip())}"
                       f"</blockquote>")
            continue

        m = re.match(r'^[-*]\s+(.*)$', stripped)
        if m:
            flush_para()
            if list_type != "ul":
                flush_list()
                out.append("<ul>")
                list_type = "ul"
            out.append(f"<li>{_inline(m.group(1))}</li>")
            continue

        m = re.match(r'^\d+\.\s+(.*)$', stripped)
        if m:
            flush_para()
            if list_type != "ol":
                flush_list()
                out.append("<ol>")
                list_type = "ol"
            out.append(f"<li>{_inline(m.group(1))}</li>")
            continue

        # standalone image line -> its own block
        if _IMG_RE.fullmatch(stripped):
            flush_para()
            flush_list()
            out.append(f"<p>{_inline(stripped)}</p>")
            continue

        para.append(stripped)

    flush_para()
    flush_list()
    return "\n".join(out)


def image_srcs(html_str: str) -> list[str]:
    """Unique image src URLs in document order."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _IMG_SRC_RE.finditer(html_str):
        u = m.group(1)
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def replace_image_srcs(html_str: str, mapping: dict[str, str]) -> str:
    """Replace <img src> URLs per mapping; drop <img> whose src maps to ''."""
    def sub(m):
        full = m.group(0)
        src = m.group(1)
        if src not in mapping:
            return full
        new = mapping[src]
        if not new:                       # upload failed -> drop the tag
            return ""
        return full.replace(f'src="{src}"', f'src="{new}"')
    return _IMG_SRC_RE.sub(sub, html_str)
