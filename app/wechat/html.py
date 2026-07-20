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
# Full <img …> tag (any attr order, optional self-close) so we can drop/replace
# the WHOLE element — matching only up to src=".." leaves trailing attrs
# (alt/style/…) leaking as visible text when an image is dropped.
_IMG_TAG_RE = re.compile(r'<img\b[^>]*?/?>', re.I)
_SRC_ATTR_RE = re.compile(r'\bsrc="([^"]*)"', re.I)


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

    i = 0
    TABLE_ROW_RE = re.compile(r'^\|(.+)\|\s*$')
    TABLE_SEP_RE = re.compile(r'^\|[-\s:|]+\|$')

    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        stripped = line.strip()
        i += 1

        if not stripped:
            flush_para()
            flush_list()
            continue

        # ── markdown table ──
        if TABLE_ROW_RE.match(stripped):
            hdr_cells = [c.strip() for c in stripped.strip("|").split("|")]
            if i < len(lines) and TABLE_SEP_RE.match(lines[i].rstrip().strip()):
                sep_raw = lines[i].rstrip().strip().strip("|")
                sep_cells = [c.strip() for c in sep_raw.split("|")]
                col_count = len(hdr_cells)
                aligns: list[str] = []
                for c in (sep_cells + ["---"] * col_count)[:col_count]:
                    if c.startswith(":") and c.endswith(":"):
                        aligns.append("center")
                    elif c.endswith(":"):
                        aligns.append("right")
                    else:
                        aligns.append("left")
                i += 1  # skip separator
                data_rows: list[list[str]] = []
                while i < len(lines):
                    r = lines[i].rstrip().strip()
                    if not TABLE_ROW_RE.match(r):
                        break
                    cells = [c.strip() for c in r.strip("|").split("|")]
                    while len(cells) < col_count:
                        cells.append("")
                    data_rows.append(cells[:col_count])
                    i += 1
                flush_para()
                flush_list()
                parts = ['<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%;margin:16px 0;font-size:15px;">']
                parts.append("<thead><tr>")
                for ci, cell in enumerate(hdr_cells):
                    al = aligns[ci] if ci < len(aligns) else "left"
                    parts.append(f'<th style="background:#07C160;color:#fff;padding:10px;text-align:{al};border:1px solid #e0e0e0;">{_html.escape(cell) or "&nbsp;"}</th>')
                parts.append("</tr></thead><tbody>")
                for ri, row in enumerate(data_rows):
                    bg = ' style="background:#f9fafb;"' if ri % 2 == 1 else ""
                    parts.append(f"<tr{bg}>")
                    for ci, cell in enumerate(row):
                        al = aligns[ci] if ci < len(aligns) else "left"
                        parts.append(f'<td style="padding:8px;border:1px solid #e0e0e0;text-align:{al};">{_inline(cell)}</td>')
                    parts.append("</tr>")
                parts.append("</tbody></table>")
                out.append("\n".join(parts))
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
    for tag in _IMG_TAG_RE.finditer(html_str):
        m = _SRC_ATTR_RE.search(tag.group(0))
        if not m:
            continue
        u = m.group(1)
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def replace_image_srcs(html_str: str, mapping: dict[str, str]) -> str:
    """Replace <img src> URLs per mapping; drop the WHOLE <img> tag when its
    src maps to '' (e.g. upload failed) so no leftover attributes leak."""
    def sub(m):
        tag = m.group(0)
        sm = _SRC_ATTR_RE.search(tag)
        if not sm:
            return tag
        src = sm.group(1)
        if src not in mapping:
            return tag
        new = mapping[src]
        if not new:                       # upload failed -> drop the whole tag
            return ""
        return tag.replace(f'src="{src}"', f'src="{new}"')
    return _IMG_TAG_RE.sub(sub, html_str)
