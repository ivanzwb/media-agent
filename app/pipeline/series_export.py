"""Compile a knowledge series into one document.

A series is written one chapter at a time and lands in the app as a set of
separate drafts, which is the right shape for publishing and the wrong shape
for reading it end to end or handing it to someone else. Compiling makes the
whole thing one file, with the chapter order, the field's shape and each
chapter's sources carried along.

Two formats, and the reason for both: Markdown keeps working in whatever the
reader edits with, while the HTML is self-contained — styles and images
included — so it reads offline and prints to PDF from any browser.
"""
from __future__ import annotations

from datetime import datetime, timezone
import html as _html
import re

from app.wechat.html import markdown_to_html

# The cross-link block earns its place in a published chapter and gets in the
# way of a compiled book, which has a table of contents instead.
_NAV_HEAD = re.compile(r"^(本文是《|Part \d+ of \d+ in the)")

_STYLE = """
:root { color-scheme: light; }
body { margin: 0 auto; padding: 48px 24px 96px; max-width: 42em;
  font: 16px/1.85 "Noto Serif SC", Georgia, "Songti SC", serif; color: #1f1f1f; }
h1, h2, h3, h4 { font-family: "Noto Sans SC", "PingFang SC", sans-serif;
  line-height: 1.35; margin: 2em 0 0.6em; }
h1 { font-size: 1.9em; } h2 { font-size: 1.45em; } h3 { font-size: 1.15em; }
p { margin: 0 0 1.1em; }
a { color: #1d39c4; word-break: break-all; }
blockquote { margin: 1.2em 0; padding: 0.6em 1.2em; border-left: 3px solid #d6e4ff;
  background: #f7f9ff; color: #444; }
code { background: #f2f3f5; padding: 0.1em 0.35em; border-radius: 4px;
  font-size: 0.92em; }
pre { background: #f7f8fa; padding: 14px 16px; border-radius: 8px;
  overflow-x: auto; } pre code { background: none; padding: 0; }
img { max-width: 100%; height: auto; }
table { border-collapse: collapse; width: 100%; margin: 1.2em 0; }
th, td { border: 1px solid #e3e3e3; padding: 8px 10px; text-align: left; }
hr { border: none; border-top: 1px solid #e8e8e8; margin: 3em 0; }
.ma-cover { padding: 18vh 0 0; text-align: center; }
.ma-cover h1 { font-size: 2.4em; margin: 0 0 0.4em; }
.ma-meta { color: #888; font-size: 0.92em; }
.ma-toc ol { padding-left: 1.4em; } .ma-toc li { margin: 0.35em 0; }
.ma-sources { font-size: 0.92em; color: #555; }
.ma-sources li { margin: 0.3em 0; }
@media print {
  body { padding: 0; max-width: none; font-size: 11.5pt; }
  a { color: inherit; text-decoration: none; }
  .ma-chapter { page-break-before: always; }
  .ma-cover { padding-top: 6cm; }
}
"""


_MAP_NODE = re.compile(r'^\s*(\w+)\["([^"]*)"\]\s*$')
_MAP_EDGE = re.compile(r"^\s*(\w+)\s*-->\s*(\w+)\s*$")


def _knowledge_outline(text: str) -> str:
    """Flatten the knowledge graph into the nested list a document can hold.

    The app draws the map as a diagram, which a Markdown file and a printed
    page cannot. The same parent-child relations read fine as an indented
    list, so the shape survives the trip even where nothing can render it.
    """
    if not (text or "").lstrip().lower().startswith(("graph", "flowchart")):
        return text or ""  # an older series stored the list itself
    labels: dict[str, str] = {}
    children: dict[str, list[str]] = {}
    parented: set[str] = set()
    for line in text.splitlines():
        node = _MAP_NODE.match(line)
        if node:
            labels.setdefault(node.group(1), node.group(2))
            continue
        edge = _MAP_EDGE.match(line)
        if edge and edge.group(1) in labels and edge.group(2) in labels:
            children.setdefault(edge.group(1), []).append(edge.group(2))
            parented.add(edge.group(2))

    lines: list[str] = []

    def walk(key: str, depth: int, seen: frozenset[str]) -> None:
        lines.append(f"{'  ' * depth}- {labels[key]}")
        if depth >= 4:
            return
        for child in children.get(key, []):
            if child not in seen:
                walk(child, depth + 1, seen | {child})

    for key in labels:
        if key not in parented:
            walk(key, 0, frozenset({key}))
    return "\n".join(lines)


def _strip_nav_note(body_md: str) -> str:
    text = (body_md or "").lstrip("\n")
    lines = text.splitlines()
    if not lines or not lines[0].startswith(">"):
        return text
    if not _NAV_HEAD.match(lines[0].lstrip("> ").strip()):
        return text
    index = 0
    while index < len(lines) and lines[index].startswith(">"):
        index += 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    return "\n".join(lines[index:])


def _demote_headings(body_md: str) -> str:
    """Push chapter headings one level down, under the chapter's own heading."""
    out: list[str] = []
    fenced = False
    for line in (body_md or "").splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and re.match(r"^#{1,5} ", line):
            line = "#" + line
        out.append(line)
    return "\n".join(out)


def _chapter_title(meta: dict, fallback: str) -> str:
    candidates = meta.get("title_candidates") or []
    return (meta.get("title_cn") or (candidates[0] if candidates else "")
            or fallback)


def compile_series(store, series_id: int) -> dict:
    """Everything a compiled series needs, in reading order."""
    series = store.get_series(series_id)
    if series is None:
        raise ValueError("系列不存在")
    titles = {row["draft_id"]: row["title"]
              for row in store.list_chapters(series_id) if row["draft_id"]}
    chapters: list[dict] = []
    for row in store.get_series_drafts(series_id):
        meta = store.read_draft_body(row["id"])
        chapters.append({
            "order": row["series_order"] or len(chapters) + 1,
            "label": meta.get("series_part_label") or "",
            "title": _chapter_title(meta, titles.get(row["id"], "")),
            "body_md": _strip_nav_note(meta.get("body_md", "")),
            "sources": meta.get("sources") or [],
        })
    if not chapters:
        raise ValueError("这个系列还没有写成的章节")
    return {
        "title": series["title"],
        "topic": series["topic"],
        "knowledge_map": _knowledge_outline(series["knowledge_map"] or ""),
        "chapters": chapters,
    }


def series_markdown(store, series_id: int) -> str:
    book = compile_series(store, series_id)
    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    parts = [f"# {book['title']}", "",
             f"> 知识系列 · 共 {len(book['chapters'])} 章 · 导出于 {today}", ""]
    parts += ["## 目录", ""]
    parts += [f"{chapter['order']}. {chapter['title']}"
              for chapter in book["chapters"]]
    if book["knowledge_map"]:
        parts += ["", "## 知识脉络", "", book["knowledge_map"]]

    for chapter in book["chapters"]:
        heading = " ".join(filter(None, [chapter["label"], chapter["title"]]))
        parts += ["", "---", "", f"## {heading}", "",
                  _demote_headings(chapter["body_md"]).strip()]
        if chapter["sources"]:
            parts += ["", "### 本章参考资料", ""]
            parts += [
                f"{index}. [{source.get('title') or source.get('url')}]"
                f"({source.get('url')})"
                + (f" — {source['source_name']}"
                   if source.get("source_name") else "")
                for index, source in enumerate(chapter["sources"], 1)]
    return "\n".join(parts).rstrip() + "\n"


def series_html(store, series_id: int, *, config=None) -> str:
    book = compile_series(store, series_id)
    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    escape = _html.escape

    body = [
        '<header class="ma-cover">',
        f"<h1>{escape(book['title'])}</h1>",
        f'<p class="ma-meta">知识系列 · 共 {len(book["chapters"])} 章 · '
        f"导出于 {today}</p>", "</header>",
        '<nav class="ma-toc"><h2>目录</h2><ol>',
    ]
    body += [f'<li><a href="#chapter-{chapter["order"]}">'
             f'{escape(chapter["title"])}</a></li>'
             for chapter in book["chapters"]]
    body.append("</ol></nav>")
    if book["knowledge_map"]:
        body.append("<section><h2>知识脉络</h2>"
                    + markdown_to_html(book["knowledge_map"]) + "</section>")

    for chapter in book["chapters"]:
        heading = " ".join(filter(None, [chapter["label"], chapter["title"]]))
        body.append(f'<section class="ma-chapter" id="chapter-'
                    f'{chapter["order"]}"><h2>{escape(heading)}</h2>')
        body.append(markdown_to_html(_demote_headings(chapter["body_md"])))
        if chapter["sources"]:
            body.append('<h3>本章参考资料</h3><ol class="ma-sources">')
            body += [
                "<li>"
                + (f'<a href="{escape(str(source.get("url") or ""), True)}">'
                   f'{escape(str(source.get("title") or source.get("url")))}'
                   "</a>")
                + (f' — {escape(str(source["source_name"]))}'
                   if source.get("source_name") else "")
                + "</li>"
                for source in chapter["sources"]]
            body.append("</ol>")
        body.append("</section>")

    document = "\n".join(body)
    if config is not None:
        # Self-contained beats small here: the file has to survive being
        # emailed around and opened with no server behind it.
        from app.wechat.publish import inline_images_base64
        document = inline_images_base64(document, config)
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="zh"><head><meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width, '
        'initial-scale=1">\n'
        f"<title>{escape(book['title'])}</title>\n"
        f"<style>{_STYLE}</style>\n</head>\n<body>\n{document}\n"
        "</body></html>\n"
    )
