"""Themed Markdown -> inline-CSS HTML for 公众号 / 头条 (spider-media logic port).

WeChat/Toutiao editors only keep INLINE styles (they strip <style>/class), so
instead of a CSS inliner (juice/premailer) we emit ``style="..."`` directly on
every element from a selectable *theme*. Ports spider-media's key fixes:
  - code blocks: join lines with <br/> + white-space:pre-wrap (editors collapse
    raw newlines inside <pre>)
  - compact lists (no whitespace between <li>)
  - glue CJK punctuation so it doesn't wrap awkwardly
  - images as centered block elements

Public API:
  render_styled_html(md, platform="wechat", theme="default", tweaks=None) -> str
  list_themes(platform) -> list[dict]   # {id, name} for the settings UI
"""
from __future__ import annotations

import html as _html
import re

from app.wechat.html import (
    _IMG_RE, _LINK_RE, _BOLD_RE, _ITALIC_RE, _CODE_RE, _VIDEO_PLACEHOLDER_RE)

# ── theme parameters ────────────────────────────────────────────────────────
# Each theme => visual knobs; styles are generated from these so tweaks
# (accent color / font size) can override cleanly.
_WECHAT_THEMES = {
    "default": {"name": "默认·清爽绿", "accent": "#07C160", "fs": 16,
                "family": "-apple-system,BlinkMacSystemFont,'PingFang SC',"
                          "'Helvetica Neue',sans-serif",
                "code_bg": "#2d2d2d", "code_fg": "#f8f8f2", "h_deco": "underline"},
    "warm": {"name": "暖橙·杂志", "accent": "#e67514", "fs": 16,
             "family": "'PingFang SC','Songti SC',Georgia,serif",
             "code_bg": "#2d2d2d", "code_fg": "#f8f8f2", "h_deco": "leftbar"},
    "blue": {"name": "科技蓝", "accent": "#2f6fb3", "fs": 16,
             "family": "-apple-system,BlinkMacSystemFont,'PingFang SC',"
                       "'Helvetica Neue',sans-serif",
             "code_bg": "#1e2430", "code_fg": "#e6edf3", "h_deco": "bordered"},
}
_TOUTIAO_THEMES = {
    "default": {"name": "头条·默认", "accent": "#f04142", "fs": 17,
                "family": "-apple-system,BlinkMacSystemFont,'PingFang SC',"
                          "'Helvetica Neue',sans-serif",
                "code_bg": "#f6f7f9", "code_fg": "#24292f", "h_deco": "plain"},
}


def _themes_for(platform: str) -> dict:
    return _TOUTIAO_THEMES if platform == "toutiao" else _WECHAT_THEMES


def list_themes(platform: str) -> list[dict]:
    return [{"id": k, "name": v["name"]}
            for k, v in _themes_for(platform).items()]


def _build_styles(t: dict) -> dict:
    accent, fs, fam = t["accent"], t["fs"], t["family"]
    code_bg, code_fg, deco = t["code_bg"], t["code_fg"], t["h_deco"]
    light_code = deco == "plain"  # toutiao-style light inline code

    if deco == "underline":
        h2 = (f"font-size:{fs+4}px;color:#222;font-weight:bold;margin:1.4em 0 .6em;"
              f"border-bottom:2px solid {accent};padding-bottom:4px;")
        h3 = f"font-size:{fs+2}px;color:{accent};font-weight:bold;margin:1.2em 0 .5em;"
    elif deco == "leftbar":
        h2 = (f"font-size:{fs+4}px;color:#333;font-weight:bold;margin:1.4em 0 .6em;"
              f"border-left:4px solid {accent};padding-left:10px;")
        h3 = f"font-size:{fs+2}px;color:{accent};font-weight:bold;margin:1.2em 0 .5em;"
    elif deco == "bordered":
        h2 = (f"font-size:{fs+4}px;color:#fff;font-weight:bold;margin:1.4em 0 .6em;"
              f"background:{accent};padding:6px 12px;border-radius:4px;")
        h3 = (f"font-size:{fs+2}px;color:{accent};font-weight:bold;margin:1.2em 0 .5em;"
              f"border-left:3px solid {accent};padding-left:8px;")
    else:  # plain
        h2 = (f"font-size:{fs+4}px;color:#222;font-weight:bold;margin:1.4em 0 .6em;"
              f"border-left:4px solid {accent};padding-left:10px;")
        h3 = f"font-size:{fs+2}px;color:{accent};font-weight:bold;margin:1.2em 0 .5em;"

    return {
        "section": (f"font-size:{fs}px;color:#333;line-height:1.75;"
                    f"font-family:{fam};letter-spacing:.3px;"
                    f"word-break:break-word;"),
        "h1": f"font-size:{fs+8}px;color:#222;font-weight:bold;margin:1.2em 0 .6em;",
        "h2": h2,
        "h3": h3,
        "h4": f"font-size:{fs+1}px;color:#444;font-weight:bold;margin:1em 0 .5em;",
        "p": "margin:0 0 16px;line-height:1.8;text-align:justify;",
        "blockquote": (f"border-left:4px solid {accent};padding:10px 15px;"
                       f"margin:16px 0;background:#f7f7f7;color:#666;"
                       f"border-radius:0 4px 4px 0;"),
        "pre": (f"background:{code_bg};color:{code_fg};padding:14px 16px;"
                f"border-radius:6px;overflow-x:auto;font-size:{fs-2}px;"
                f"line-height:1.5;margin:16px 0;white-space:pre-wrap;"
                f"word-break:break-all;"
                + ("border:1px solid #e6e8eb;" if light_code else "")),
        "code_block": f"color:{code_fg};white-space:pre-wrap;"
                      "font-family:Menlo,Consolas,monospace;",
        "code_inline": ("background:#f2f3f5;color:#c0341d;padding:2px 5px;"
                        "border-radius:3px;font-size:.92em;"
                        "font-family:Menlo,Consolas,monospace;"),
        "ul": "padding-left:1.6em;margin:12px 0;",
        "ol": "padding-left:1.6em;margin:12px 0;",
        "li": "margin:6px 0;line-height:1.8;",
        "img": "max-width:100%;display:block;margin:14px auto;border-radius:6px;",
        "a": f"color:{accent};text-decoration:none;border-bottom:1px solid {accent};",
        "hr": "border:none;border-top:1px solid #e0e0e0;margin:24px 0;",
        "mark": "background:#fff3a0;color:#664d03;padding:0 .15em;border-radius:2px;",
        "accent": accent,
        # table styles
        "table": ("border-collapse:collapse;width:100%;margin:16px 0;"
                  "font-size:15px;overflow-x:auto;"),
        "th": (f"background:{accent};color:#fff;padding:10px 12px;"
               "text-align:left;font-weight:bold;border:1px solid #e0e0e0;"),
        "td": ("padding:8px 12px;border:1px solid #e0e0e0;"
               "text-align:left;line-height:1.7;"),
        "tr_even": "background:#f9fafb;",
    }


# ── inline rendering (styled) ────────────────────────────────────────────────
def _inline(text: str, st: dict) -> str:
    def img_sub(m):
        alt = _html.escape(m.group("alt"))
        url = _html.escape(m.group("url"), quote=True)
        return f'<img src="{url}" alt="{alt}" style="{st["img"]}"/>'
    text = _IMG_RE.sub(img_sub, text)

    tags: list[str] = []

    def stash(m):
        tags.append(m.group(0))
        return f"\x00{len(tags) - 1}\x00"
    text = re.sub(r'<img[^>]*/>', stash, text)
    text = _html.escape(text)

    def link_sub(m):
        label = m.group("text")
        url = _html.escape(m.group("url"), quote=True)
        return f'<a href="{url}" style="{st["a"]}">{label}</a>'
    text = _LINK_RE.sub(link_sub, text)
    text = _BOLD_RE.sub(lambda m: f"<strong>{m.group('t')}</strong>", text)
    text = _ITALIC_RE.sub(lambda m: f"<em>{m.group('t')}</em>", text)
    text = _CODE_RE.sub(
        lambda m: f'<code style="{st["code_inline"]}">{m.group("t")}</code>', text)
    # ==highlight== → styled <mark>
    text = re.sub(r'==(.+?)==',
                  lambda m: f'<mark style="{st["mark"]}">{m.group(1)}</mark>',
                  text)
    # {color:#hex}文字{/color} → colored span
    text = re.sub(
        r'\{color:(#[0-9a-fA-F]{3,8}|[a-zA-Z]+)\}(.+?)\{/color\}',
        lambda m: f'<span style="color:{m.group(1)}">{m.group(2)}</span>', text)
    text = re.sub(r'\x00(\d+)\x00', lambda m: tags[int(m.group(1))], text)
    return text


_CJK = r'\u4e00-\u9fff\u3000-\u303f\uff00-\uffef'


def _glue_cjk_punctuation(html_str: str) -> str:
    """Keep CJK punctuation attached to the preceding char/tag so the editor
    doesn't wrap a lone punctuation mark to the next line."""
    return re.sub(r'\s+([' + _CJK + r'])', r'\1', html_str)


# ── markdown table parsing ────────────────────────────────────────────────────

_TABLE_ROW_RE = re.compile(r'^\|(.+)\|\s*$')
_TABLE_SEP_RE = re.compile(r'^\|[-\s:|]+\|$')


def _is_table_separator(line: str) -> bool:
    """A line like |---|---| or | :--- | ---: |."""
    return bool(re.match(r'^\|[-\s:|]+\|$', line.strip()))


def _parse_table_cells(line: str) -> list[str]:
    """Split a table row `| a | b | c |` into cells, trimming whitespace."""
    stripped = line.strip().strip("|")
    return [c.strip() for c in stripped.split("|")]


def _render_table(lines: list[str], start: int, st: dict) -> tuple[str, int]:
    """Try to parse a markdown table starting at *start*.

    Returns (html_fragment, next_index) on success, or ("", start) if the
    lines don't form a valid table.
    """
    n = len(lines)
    if start >= n:
        return "", start
    # First line must be a header row with at least 2 cells
    hdr = lines[start].strip()
    if not _TABLE_ROW_RE.match(hdr):
        return "", start
    header_cells = _parse_table_cells(hdr)
    if len(header_cells) < 2:
        return "", start
    # Second line must be a separator row
    if start + 1 >= n or not _is_table_separator(lines[start + 1].strip()):
        return "", start
    sep_cells = _parse_table_cells(lines[start + 1].strip())
    col_count = len(header_cells)
    if len(sep_cells) != col_count:
        return "", start
    # Parse alignment hints from separator
    aligns: list[str] = []
    for c in sep_cells:
        c = c.strip().strip(":")
        if c.startswith("-") and c.endswith("-"):
            left = sep_cells[sep_cells.index(c)] if sep_cells.index(c) < len(sep_cells) else ""
            left_stripped = left.strip()
            if left_stripped.startswith(":") and left_stripped.endswith(":"):
                aligns.append("center")
            elif left_stripped.endswith(":"):
                aligns.append("right")
            else:
                aligns.append("left")
    # Actually, let me parse aligns properly
    aligns = []
    raw_sep = [c.strip() for c in lines[start + 1].strip().strip("|").split("|")]
    # Re-do proper parsing
    raw = lines[start + 1].strip()
    raw = raw.strip("|")
    raw_cells = [c.strip() for c in raw.split("|")]
    for c in raw_cells:
        if c.startswith(":") and c.endswith(":"):
            aligns.append("center")
        elif c.endswith(":"):
            aligns.append("right")
        else:
            aligns.append("left")
    while len(aligns) < col_count:
        aligns.append("left")
    aligns = aligns[:col_count]

    # Collect data rows
    i = start + 2
    data_rows: list[list[str]] = []
    while i < n:
        row = lines[i].strip()
        if not _TABLE_ROW_RE.match(row):
            break
        cells = _parse_table_cells(row)
        # Pad or truncate to match header
        while len(cells) < col_count:
            cells.append("")
        cells = cells[:col_count]
        data_rows.append(cells)
        i += 1

    # Render HTML table
    parts: list[str] = [f'<table style="{st["table"]}">']
    # Header
    parts.append("<thead>")
    parts.append("<tr>")
    for idx, cell in enumerate(header_cells):
        al = aligns[idx] if idx < len(aligns) else "left"
        parts.append(f'<th style="{st["th"]};text-align:{al}">{_html.escape(cell) or "&nbsp;"}</th>')
    parts.append("</tr>")
    parts.append("</thead>")
    # Body
    parts.append("<tbody>")
    for ri, row in enumerate(data_rows):
        tr_style = st["tr_even"] if ri % 2 == 1 else ""
        parts.append(f'<tr style="{tr_style}">' if tr_style else "<tr>")
        for idx, cell in enumerate(row):
            al = aligns[idx] if idx < len(aligns) else "left"
            parts.append(
                f'<td style="{st["td"]};text-align:{al}">'
                f'{_inline(cell, st) or "&nbsp;"}</td>')
        parts.append("</tr>")
    parts.append("</tbody>")
    parts.append("</table>")

    return "\n".join(parts), i


# ── directive containers (:::type ... :::) ───────────────────────────────
_CONTAINER_OPEN_RE = re.compile(r'^:::\s*([\w-]+)\s*(.*)$')
_CONTAINER_CLOSE_RE = re.compile(r'^:::\s*$')
_BOX_PALETTES = {
    "tip":     ("#f0f9eb", "#67c23a", "#3c6e2a"),
    "info":    ("#eef5ff", "#409eff", "#2a5b9e"),
    "warning": ("#fdf6ec", "#e6a23c", "#8a5a1a"),
    "success": ("#f0f9eb", "#67c23a", "#3c6e2a"),
    "danger":  ("#fef0f0", "#f56c6c", "#a13a3a"),
    "note":    ("#f7f7f9", "#909399", "#555555"),
}

# ── decorative components (dividers / titles / image card) ───────────────
# Fixed-color, theme-independent styles so the frontend live preview
# (frontend/src/lib/mdPreview.tsx) can reproduce them BYTE-IDENTICALLY for
# strict WYSIWYG. Keep each string in lock-step with its mdPreview.tsx twin.
_DIVIDER_DASHED_STYLE = "border:none;border-top:1px dashed #c8c8c8;height:0;margin:22px 0;"
_DIVIDER_GRADIENT_STYLE = (
    "border:none;height:3px;margin:22px 0;border-radius:2px;"
    "background:linear-gradient(to right,rgba(64,158,255,0),#409eff,rgba(64,158,255,0));")
_BLOCKTITLE_STYLE = (
    "background:#2f6fb3;color:#ffffff;padding:8px 16px;border-radius:6px;"
    "text-align:center;font-weight:bold;font-size:17px;margin:16px 0;")
_DUALLINE_STYLE = (
    "border-top:2px solid #333333;border-bottom:2px solid #333333;padding:8px 0;"
    "text-align:center;font-weight:bold;font-size:18px;color:#222222;margin:18px 0;")
_TITLENUM_WRAP_STYLE = "margin:18px 0 10px;"
_TITLENUM_BADGE_STYLE = (
    "display:inline-block;min-width:26px;height:26px;line-height:26px;text-align:center;"
    "background:#2f6fb3;color:#ffffff;border-radius:13px;font-weight:bold;font-size:15px;"
    "margin-right:10px;padding:0 6px;")
_TITLENUM_TEXT_STYLE = "font-size:18px;font-weight:bold;color:#222222;vertical-align:middle;"
_IMGCARD_FRAME_STYLE = (
    "border:1px solid #e6e8eb;border-radius:10px;overflow:hidden;margin:16px 0;"
    "box-shadow:0 2px 12px rgba(0,0,0,0.08);background:#ffffff;")
_IMGCARD_IMG_STYLE = "display:block;width:100%;margin:0;border-radius:0;"
_IMGCARD_CAP_STYLE = "margin:0;padding:8px 12px;font-size:13px;color:#888888;text-align:center;"


def _render_imgcard(inner_lines: list[str], st: dict) -> str:
    """Framed image card: rounded/shadowed border wrapping an image + caption."""
    img_html = ""
    caption_parts: list[str] = []
    for ln in inner_lines:
        s = ln.strip()
        if not s:
            continue
        m = _IMG_RE.search(s)
        if m and not img_html:
            alt = _html.escape(m.group("alt"))
            url = _html.escape(m.group("url"), quote=True)
            img_html = f'<img src="{url}" alt="{alt}" style="{_IMGCARD_IMG_STYLE}"/>'
        else:
            caption_parts.append(s)
    cap_html = ""
    if caption_parts:
        cap_html = (f'<p style="{_IMGCARD_CAP_STYLE}">'
                    f'{_inline(" ".join(caption_parts), st)}</p>')
    return f'<section style="{_IMGCARD_FRAME_STYLE}">{img_html}{cap_html}</section>'


def _render_container(ctype: str, inner_lines: list[str], st: dict,
                      params: dict) -> str:
    accent = params["accent"]
    fs = params["fs"]
    if ctype in ("center", "right"):
        align = "center" if ctype == "center" else "right"
        inner = _render_blocks(inner_lines, st, params, align=align)
        return f'<div style="text-align:{align};">{inner}</div>'
    if ctype == "footer":
        inner = _render_blocks(inner_lines, st, params, align="center")
        return (f'<div style="text-align:center;color:#999;'
                f'font-size:{fs - 2}px;margin:18px 0 8px;">{inner}</div>')
    if ctype == "button":
        label, url = "按钮", "#"
        for ln in inner_lines:
            s = ln.strip()
            if not s:
                continue
            if "|" in s:
                label, url = s.split("|", 1)
            else:
                label = s
            break
        label = _html.escape(label.strip())
        url = _html.escape(url.strip(), quote=True)
        btn = (f"display:inline-block;padding:10px 32px;background:{accent};"
               "color:#fff;border-radius:24px;text-decoration:none;font-size:15px;")
        return (f'<div style="text-align:center;margin:20px 0;">'
                f'<a href="{url}" style="{btn}">{label}</a></div>')
    if ctype == "tags":
        pill = ("display:inline-block;padding:3px 12px;margin:4px 6px 4px 0;"
                f"background:#f5f7fa;color:{accent};border:1px solid {accent};"
                "border-radius:12px;font-size:13px;")
        pills = [f'<span style="{pill}">{_html.escape(ln.strip())}</span>'
                 for ln in inner_lines if ln.strip()]
        return f'<div style="margin:12px 0;">{"".join(pills)}</div>'
    # ── decorative dividers (no body) ──
    if ctype == "dashed":
        return f'<div style="{_DIVIDER_DASHED_STYLE}"></div>'
    if ctype == "gradient":
        return f'<div style="{_DIVIDER_GRADIENT_STYLE}"></div>'
    # ── decorative section titles ──
    if ctype in ("blocktitle", "dualline"):
        text = _inline(" ".join(l.strip() for l in inner_lines if l.strip()), st)
        style = _BLOCKTITLE_STYLE if ctype == "blocktitle" else _DUALLINE_STYLE
        return f'<section style="{style}">{text}</section>'
    if ctype == "titlenum":
        raw = next((l.strip() for l in inner_lines if l.strip()), "")
        num, sep, title = raw.partition("|")
        if not sep:
            num, title = "1", raw
        badge = _html.escape(num.strip()) or "1"
        return (f'<section style="{_TITLENUM_WRAP_STYLE}">'
                f'<span style="{_TITLENUM_BADGE_STYLE}">{badge}</span>'
                f'<span style="{_TITLENUM_TEXT_STYLE}">'
                f'{_inline(title.strip(), st)}</span></section>')
    if ctype == "imgcard":
        return _render_imgcard(inner_lines, st)
    # box-style cards
    if ctype == "highlight":
        box = (f"background:#fafafa;border-left:4px solid {accent};"
               "padding:14px 16px;margin:16px 0;border-radius:0 6px 6px 0;color:#333;")
    elif ctype == "card":
        box = ("background:#ffffff;border:1px solid #e6e8eb;color:#333;"
               "padding:14px 16px;margin:16px 0;border-radius:8px;")
    else:
        bg, border, fg = _BOX_PALETTES.get(ctype, ("#f7f7f9", accent, "#444444"))
        box = (f"background:{bg};border-left:4px solid {border};color:{fg};"
               "padding:12px 16px;margin:16px 0;border-radius:0 6px 6px 0;")
    inner = _render_blocks(inner_lines, st, params)
    return f'<section style="{box}">{inner}</section>'


# ── multi-column layout (::::columns / :::col) ───────────────────────────
# 秀米-style side-by-side columns that survive WeChat's sanitizer (WeChat keeps
# flex on <section>). FOUR colons open the outer container so ordinary 3-colon
# directives (:::tip …) nest cleanly inside a column without marker ambiguity.
#
#   ::::columns          equal-width columns (count = number of :::col blocks)
#   ::::columns 1:2      custom flex ratios per column (left:right → flex:1/flex:2)
#   ::::columns cols=3   explicit equal-width count (ratios all 1)
#   :::col … :::         one column body (may hold markdown / nested :::directives)
#   ::::                 closes the columns container
_COLUMNS_OPEN_RE = re.compile(r'^::::\s*columns\b\s*(.*)$')
_COLUMNS_CLOSE_RE = re.compile(r'^::::\s*$')
_COL_OPEN_RE = re.compile(r'^:::\s*col\b\s*(.*)$')
# Shared column styling (kept byte-identical with frontend mdPreview.tsx).
_COLUMNS_ROW_STYLE = "display:flex;gap:12px;margin:16px 0;align-items:flex-start;"


def _parse_col_ratios(param: str) -> list[int]:
    """Parse a ``::::columns`` param into per-column flex weights.

    '1:2' -> [1, 2];  'cols=3' -> [1, 1, 1];  '' / junk -> [] (equal default).
    """
    param = (param or "").strip()
    if not param:
        return []
    m = re.fullmatch(r'cols\s*=\s*(\d+)', param)
    if m:
        return [1] * max(1, int(m.group(1)))
    if re.fullmatch(r'\d+(\s*:\s*\d+)*', param):
        return [max(1, int(x)) for x in re.split(r'\s*:\s*', param)]
    return []


def _split_col_blocks(lines: list[str]) -> list[list[str]]:
    """Split columns-container inner lines into per-column line lists using the
    ``:::col … :::`` markers (3-colon depth-tracked so nested :::tip etc. close
    correctly and don't leak into the next column)."""
    cols: list[list[str]] = []
    i, n = 0, len(lines)
    while i < n:
        if not _COL_OPEN_RE.match(lines[i].strip()):
            i += 1
            continue
        i += 1
        body: list[str] = []
        depth = 1
        while i < n:
            s = lines[i].strip()
            if _CONTAINER_CLOSE_RE.match(s):
                depth -= 1
                if depth == 0:
                    i += 1
                    break
                body.append(lines[i])
                i += 1
                continue
            nested = _CONTAINER_OPEN_RE.match(s)
            if nested and nested.group(1):
                depth += 1
            body.append(lines[i])
            i += 1
        cols.append(body)
    return cols


def _render_columns(param: str, inner_lines: list[str], st: dict,
                    params: dict) -> str:
    """Render a ``::::columns`` container to a WeChat-safe flex row of sections.
    Each column body is rendered recursively (full markdown + nested directives)."""
    col_blocks = _split_col_blocks(inner_lines)
    if not col_blocks:
        return ""
    ratios = _parse_col_ratios(param)
    cells: list[str] = []
    for idx, body in enumerate(col_blocks):
        flex = ratios[idx] if idx < len(ratios) else 1
        inner = _render_blocks(body, st, params)
        cells.append(
            f'<section style="flex:{flex};min-width:0;">{inner}</section>')
    return f'<section style="{_COLUMNS_ROW_STYLE}">{"".join(cells)}</section>'


def _render_blocks(lines: list[str], st: dict, params: dict,
                   align: str | None = None) -> str:
    """Render a list of markdown lines to HTML fragments (no outer <section>).
    Recursively invoked for directive containers."""
    out: list[str] = []
    para: list[str] = []
    list_type: str | None = None
    i = 0
    p_style = st["p"]
    if align:
        p_style = f"{p_style}text-align:{align};"

    def flush_para():
        if para:
            out.append(f'<p style="{p_style}">'
                       + "<br/>".join(_inline(x, st) for x in para) + "</p>")
            para.clear()

    def flush_list():
        nonlocal list_type
        if list_type:
            out.append(f"</{list_type}>")
            list_type = None

    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        i += 1

        if not stripped:
            flush_para()
            flush_list()
            continue

        # ── multi-column container (::::columns … ::::) ──
        colo = _COLUMNS_OPEN_RE.match(stripped)
        if colo:
            flush_para()
            flush_list()
            param = colo.group(1).strip()
            col_inner: list[str] = []
            depth = 1
            while i < len(lines):
                s = lines[i].strip()
                if _COLUMNS_CLOSE_RE.match(s):
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                    col_inner.append(lines[i])
                    i += 1
                    continue
                if _COLUMNS_OPEN_RE.match(s):
                    depth += 1
                col_inner.append(lines[i])
                i += 1
            out.append(_render_columns(param, col_inner, st, params))
            continue

        # ── directive container (:::type ... :::) ──
        cm = _CONTAINER_OPEN_RE.match(stripped)
        if cm and cm.group(1):
            flush_para()
            flush_list()
            ctype = cm.group(1)
            first_arg = cm.group(2).strip()
            inner: list[str] = []
            if first_arg:
                inner.append(first_arg)
            depth = 1
            while i < len(lines):
                ln = lines[i].strip()
                if _CONTAINER_CLOSE_RE.match(ln):
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                    inner.append(lines[i])
                    i += 1
                    continue
                nested = _CONTAINER_OPEN_RE.match(ln)
                if nested and nested.group(1):
                    depth += 1
                inner.append(lines[i])
                i += 1
            out.append(_render_container(ctype, inner, st, params))
            continue

        # ── markdown table ──
        table_html, next_i = _render_table(lines, i - 1, st)
        if next_i > i - 1:
            flush_para()
            flush_list()
            out.append(table_html)
            i = next_i
            continue

        # fenced code block
        m = re.match(r'^```+\s*([\w+-]*)\s*$', stripped)
        if m:
            flush_para()
            flush_list()
            buf: list[str] = []
            while i < len(lines) and not re.match(r'^```+\s*$', lines[i].strip()):
                buf.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            body = "<br/>".join(_html.escape(l) or "&nbsp;" for l in buf)
            out.append(f'<pre style="{st["pre"]}">'
                       f'<code style="{st["code_block"]}">{body}</code></pre>')
            continue

        if stripped.startswith("<iframe") or stripped.startswith("[▶"):
            flush_para()
            flush_list()
            continue

        m = re.match(r'^(#{1,6})\s+(.*)$', stripped)
        if m:
            flush_para()
            flush_list()
            lvl = min(len(m.group(1)), 4)
            out.append(f'<h{lvl} style="{st["h" + str(lvl)]}">'
                       f'{_inline(m.group(2), st)}</h{lvl}>')
            continue

        if re.match(r'^(-{3,}|\*{3,}|_{3,})$', stripped):
            flush_para()
            flush_list()
            out.append(f'<hr style="{st["hr"]}"/>')
            continue

        if stripped.startswith(">"):
            flush_para()
            flush_list()
            out.append(f'<blockquote style="{st["blockquote"]}">'
                       f'{_inline(stripped[1:].strip(), st)}</blockquote>')
            continue

        m = re.match(r'^[-*]\s+(.*)$', stripped)
        if m:
            flush_para()
            if list_type != "ul":
                flush_list()
                out.append(f'<ul style="{st["ul"]}">')
                list_type = "ul"
            out.append(f'<li style="{st["li"]}">{_inline(m.group(1), st)}</li>')
            continue

        m = re.match(r'^\d+\.\s+(.*)$', stripped)
        if m:
            flush_para()
            if list_type != "ol":
                flush_list()
                out.append(f'<ol style="{st["ol"]}">')
                list_type = "ol"
            out.append(f'<li style="{st["li"]}">{_inline(m.group(1), st)}</li>')
            continue

        if _IMG_RE.fullmatch(stripped):
            flush_para()
            flush_list()
            out.append(f'<p style="text-align:center;margin:14px 0;">'
                       f'{_inline(stripped, st)}</p>')
            continue

        para.append(stripped)

    flush_para()
    flush_list()
    return "\n".join(out)


def render_styled_html(md: str, platform: str = "wechat",
                       theme: str = "default", tweaks: dict | None = None) -> str:
    """Render Markdown to themed, inline-styled HTML for the platform editor."""
    themes = _themes_for(platform)
    params = dict(themes.get(theme) or next(iter(themes.values())))
    tweaks = tweaks or {}
    if tweaks.get("accent"):
        params["accent"] = tweaks["accent"]
    if tweaks.get("font_size"):
        try:
            params["fs"] = int(tweaks["font_size"])
        except (TypeError, ValueError):
            pass
    st = _build_styles(params)

    md = _VIDEO_PLACEHOLDER_RE.sub("", md or "")
    lines = md.replace("\r\n", "\n").split("\n")
    body = _render_blocks(lines, st, params)
    body = _glue_cjk_punctuation(body)
    return f'<section style="{st["section"]}">\n{body}\n</section>'
