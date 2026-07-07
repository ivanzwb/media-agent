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
    text = re.sub(r'\x00(\d+)\x00', lambda m: tags[int(m.group(1))], text)
    return text


_CJK = r'\u4e00-\u9fff\u3000-\u303f\uff00-\uffef'


def _glue_cjk_punctuation(html_str: str) -> str:
    """Keep CJK punctuation attached to the preceding char/tag so the editor
    doesn't wrap a lone punctuation mark to the next line."""
    return re.sub(r'\s+([' + _CJK + r'])', r'\1', html_str)


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
    out: list[str] = []
    para: list[str] = []
    list_type: str | None = None
    i = 0

    def flush_para():
        if para:
            out.append(f'<p style="{st["p"]}">'
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

        if not stripped:
            flush_para()
            flush_list()
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
    body = "\n".join(out)
    body = _glue_cjk_punctuation(body)
    return f'<section style="{st["section"]}">\n{body}\n</section>'
