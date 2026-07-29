from __future__ import annotations

import html
import json
import re
from urllib.parse import urlsplit

from app.llm.base import LLMProvider, Message
from app.models import Article, Draft
from app.pipeline.anti_slop import ANTI_SLOP_SYSTEM_INSTRUCTION, post_process

REWRITE_SYSTEM = (
    "你是资深自媒体编辑，负责将原文改写为深度中文自媒体报道。"
    "改写后的文章应像行业顶尖媒体的作品。"
    "基于且仅基于提供的原文事实进行改写——绝不可编造原文没有的数据、引用、结论或时间。"
    "目标：用最通俗的语言讲最专业的内容，让每个普通读者也能看懂。"
    "绝不在原文基础上添加夸张描述或自己的判断。"
    "不确定的内容不要写。\n\n"
    "⚠️ 只输出到 stdout，不得在磁盘上创建或修改任何文件。\n\n"
    + ANTI_SLOP_SYSTEM_INSTRUCTION
)

REWRITE_INSTRUCTION = (
    "分两步完成：\n\n"
    "**第一步 — 完整理解**：通读全文，确保理解所有技术细节、数据、结论和引用。\n\n"
    "**第二步 — 深度报道转写**：在理解的基础上，写成中文深度报道。\n\n"
    "### 文章结构（必遵守）\n\n"
    "正文用 **中文数字编号章节** 组织，格式如下：\n\n"
    "---\n"
    "## 一、[场景/问题引入——用故事或问句吸引读者]\n"
    "## 二、[背景展开——解释为什么这个问题重要]\n"
    "## 三、[深入/类比——用贴切的比喻或案例让读者理解]\n"
    "## 四、[数据/论据——用原文的数据和研究支撑观点]\n"
    "## 五、[方案/产品介绍——如实介绍原文提到的方案]\n"
    "## 写在最后\n\n"
    "注意：\n"
    "- 章节数量根据原文内容灵活调整，可以 4-6 章，但必须有 `写在最后` 作为结尾章节\n"
    "- 章节标题用中文数字编号（一、二、三…），不要用 emoji 做标题\n"
    "- 标题要有点击欲但不浮夸，用问句最佳\n\n"
    "### 开头要求\n\n"
    "- **必须有钩子**：前 1-2 句话抓住读者\n"
    "- 可用问句开头（如「你有没有想过一个问题？」「你知道 X 有多重要吗？」）\n"
    "- 或惊人场景/数据开头\n"
    "- 不要用「想象一下：」这种俗套句式\n"
    "- 第一段场景感要强，让读者有代入感\n\n"
    "### 正文风格\n\n"
    "- **讲故事**，不是列知识点。每章从故事/场景/问题切入，再展开说明\n"
    "- **用比喻、类比**来解释复杂概念——这是好文章的亮点\n"
    "- **短句、短段落**（不超过 4 行），手机友好\n"
    "- **数据全部保留**，这是硬价值\n"
    "- **专业术语**首次出现时用通俗语言解释，括号标注英文\n"
    "- 技术性太强的段落提炼核心观点，不要逐字翻译\n"
    "- 引用的观点/数据准确传达原文意思，不要曲解\n"
    "- 原文的表格/列表转为中文表格（如产品对比）或 bullet points\n\n"
    "### Emoji 规则（严格）\n\n"
    "- **章节标题里不要用 emoji**\n"
    "- 正文中可适当点缀，但不要过度（一篇最多 3-5 个）\n"
    "- 唯一可以在「写在最后」章节适当多用，用来画重点（如 👤 🤖 ⚙️）\n\n"
    "### 结尾「写在最后」章节\n\n"
    "必须包含以下内容（按顺序）：\n"
    "1. **总结**：全文的主线观点或启示\n"
    "2. **展望**：对未来的预测或思考（基于原文，不得编造）\n"
    "3. **原文信息**：注明原文标题、来源和日期\n"
    "{{seo_block}}\n"
    "{{promotion_block}}\n\n"
    "### 翻译原则\n\n"
    "| 原文情况 | 处理方式 |\n"
    "|---------|---------|\n"
    "| 好的比喻/类比 | 保留并强化，这是文章的亮点 |\n"
    "| 专业术语 | 首次出现时用通俗语言解释，可加括号标注英文 |\n"
    "| 长难句 | 拆成 2-3 个短句 |\n"
    "| 列表/表格 | 转为中文表格（产品对比必用表格）或 bullet points |\n"
    "| 数据/指标 | 全部保留，这是文章的硬价值 |\n"
    "| 技术性太强的段落 | 提炼核心观点，不要逐字翻译 |\n"
    "| 英文引用/专有名词 | 保留英文，补充中文说明 |\n"
    "| 作者观点 | 准确传达，不要曲解 |\n\n"
    "### 严禁\n\n"
    "- 不得添加原文没有的比喻、场景、数据、引用\n"
    "- 不得添加原文没有的「想象场景」或细节描写\n"
    "- 不得夸大原文的数字、结论或影响力\n"
    "- 不得添加个人评价（如「这是一个了不起的创新」）\n"
    "- 不得不确定原文是否提及就写「据 X 报道/分析」\n"
    "- 引用原文一定要准确对应，不要混淆不同来源的信息\n"
    "- 严禁 AI 腔：不要空泛开头（随着、近年来）、不要假深刻（不仅是…更是）、"
    "不要报告套话（赋能、抓手、闭环）、不要科技博客体（干货满满、保姆级教程）、"
    "不要公众号体（愿我们都能）、不要假口语（说实话、懂的都懂）、"
    "不要机械结构（总的来说、综上所述）、不要结尾升华。\n\n"
    "### 图文结合\n\n"
    "{manifest}\n\n"
    "### 图片/视频插入规则\n\n"
    "- 原文中的图片已经用 `![](url)` 格式嵌入在正文中了。"
    "在改写时，**保留这些 `![](url)` 在原文中出现的位置附近**，不要全部移到文末。\n"
    "- 保留最相关的 **2-4 张** 图片，每张插在与内容最相关的段落之间\n"
    "- 如果有原文视频（<iframe>），也必须在相关内容附近插入 `[[VID:0]]`，不要堆在开头或结尾\n"
    "- **禁止用 `![](url)` 表示视频**；Markdown 图片语法只用于图片，"
    "视频必须使用对应的 `[[VID:N]]` 占位符\n"
    "- 每张图/每个视频要与附近文字内容相关\n"
    "- 如果某张图/视频跟内容不相关就删掉，不要硬塞\n"
    "- 如果一个 `![](url)` 已经在正文中合适的位置，就保留它\n\n"
    "输出严格的 JSON，字段：\n"
    '  "title_candidates": [3 个吸睛但不虚假的标题],\n'
    '  "body_md": "Markdown 正文（中文数字编号章节+短段落+'
    '图片用 ![](url) 保留或 [[IMG:0]] 短占位符、视频用 [[VID:0]] 插入）"\n'
    '⚠️ 关键：你的回复必须从 `{{` 开始、以 `}}` 结束，不要加任何前缀、'
    "后缀、解释、摘要或代码围栏。body_md 字段中的换行必须用 \\n 转义，"
    '不能出现真正的换行符。不要写\u201c已完成\u201d\u201c已输出至文件\u201d等元评论。\n\n"'
    "原文标题：{title}\n来源：{source}\n\n原文正文：\n{content}"
)

CHECK_INSTRUCTION = (
    "你是事实校验员。对照原文，找出改写稿中原文未提及或可能失真的具体说法。\n"
    "输出严格 JSON：{{\"flagged_claims\": [可疑说法的简短描述]}}。"
    "若全部忠实于原文，返回空数组。不要输出 JSON 以外内容。\n"
    "⚠️ 只输出到 stdout，不得在磁盘上创建或修改任何文件。\n\n"
    "原文：\n{source_content}\n\n改写稿：\n{draft}"
)


_MAX_IMG = 12
_MAX_VID = 8
_PLACEHOLDER_RE = re.compile(
    r"\[{1,2}\s*(IMG|VID|VIDEO)\s*:\s*(\d+)\s*\]{1,2}", re.I)
_MD_IMAGE_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\((?P<url>[^)\s]+)(?:\s+[^)]*)?\)")


def _unique(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _hint(url: str) -> str:
    """A short, human-readable hint for an asset (its file name)."""
    tail = url.rstrip("/").split("/")[-1].split("?")[0]
    return (tail or url)[:60]


def _relativize_media(url: str) -> str:
    """Convert absolute /media/ or /images/ paths to relative paths so
    markdown files work both in the web app and in external MD editors.

    /media/abc/pic.png → ../../media/abc/pic.png
    """
    if url.startswith("/media/"):
        return f"../../media/{url[len('/media/'):]}"
    if url.startswith("/images/"):
        return f"../../images/{url[len('/images/'):]}"
    return url


def _video_embed(url: str) -> str:
    url = (url or "").strip()
    if any(ord(char) < 32 for char in url):
        return ""
    parsed = urlsplit(url)
    is_remote = parsed.scheme in ("http", "https") and bool(parsed.netloc)
    is_local = url.startswith(("/media/", "/videos/", "../../media/",
                               "../../videos/"))
    if not (is_remote or is_local):
        return ""
    safe_url = html.escape(url, quote=True)
    return (f'<iframe src="{safe_url}" width="100%" height="420" '
            'frameborder="0" allowfullscreen></iframe>')


def _build_manifest(images: list[str], videos: list[str]):
    """Return (manifest_text, media_map) for placeholder-based insertion.

    Uses zero-indexed colon format (``[[IMG:0]]``) matching the extractor's
    output so the LLM sees consistent tokens in the source body and can
    reproduce them in its output.  media_map maps a token like ``"IMG0"`` ->
    ``("img"|"vid", url)``.

    The manifest text now primarily describes the images for the LLM's
    awareness — they are already pre-expanded as ``![](url)`` inline in
    the source body by the time the LLM sees them.
    """
    imgs = _unique(images or [])[:_MAX_IMG]
    vids = _unique(videos or [])[:_MAX_VID]
    media_map: dict[str, tuple[str, str]] = {}
    lines: list[str] = []
    if imgs:
        lines.append(
            "【可用配图】已用 `![](url)` 格式嵌入在原文正文中。"
            "改写时根据内容相关性保留（与原文位置相近、与段落内容相关），"
            "不相关可删除：")
        for i, u in enumerate(imgs):
            tok = f"IMG{i}"
            media_map[tok] = ("img", u)
            lines.append(f"- {_hint(u)}")
    if vids:
        lines.append("【可用视频】用 [[VID:0]] 占位符插入正文最相关位置：")
        for i, u in enumerate(vids):
            tok = f"VID{i}"
            media_map[tok] = ("vid", u)
            lines.append(f"  [[VID:{i}]] - {_hint(u)}")
    if media_map:
        lines.append(
            "规则：不要编造不存在的图片或视频，不要把所有媒体堆在文末，"
            "确保每张图/视频与附近文字内容相关。")
    return "\n".join(lines), media_map


def _apply_placeholders(body: str, media_map: dict[str, tuple[str, str]]) -> str:
    """Replace [[IMG1]]/[[VID1]] placeholders with real markdown/embeds.

    Unknown or leftover placeholders are removed.
    """
    def repl(m: re.Match) -> str:
        kind = m.group(1).upper()
        tok = f"{'VID' if kind == 'VIDEO' else kind}{m.group(2)}"
        item = media_map.get(tok)
        if not item:
            return ""
        kind, url = item
        if kind == "img":
            return f"\n\n![]({url})\n\n"
        return f"\n\n{_video_embed(url)}\n\n"

    return _PLACEHOLDER_RE.sub(repl, body)


def _normalize_video_markdown(body: str, videos: list[str]) -> str:
    """Turn image-style Markdown for known videos into playable embeds.

    LLMs occasionally emit ``![](clip.mp4)`` even though the prompt requests a
    video placeholder.  Keeping that syntax produces a broken image and also
    fools media de-duplication because the URL is already present.
    """
    known: set[str] = set()
    for url in _unique(videos or []):
        known.add(url)
        known.add(_relativize_media(url))

    def repl(match: re.Match) -> str:
        url = match.group("url")
        if url not in known:
            return match.group(0)
        return f"\n\n{_video_embed(url)}\n\n"

    return _MD_IMAGE_RE.sub(repl, body)


def _video_is_embedded(body: str, url: str) -> bool:
    """Return whether *url* already appears in a real video/embed element."""
    for candidate in (url, _relativize_media(url)):
        for rendered in {candidate, html.escape(candidate, quote=True)}:
            escaped = re.escape(rendered)
            if re.search(
                rf'<(?:iframe|video)\b[^>]*\bsrc=["\']{escaped}["\']',
                body, re.I,
            ):
                return True
    return False


def _media_block(images: list[str], videos: list[str], existing: str) -> str:
    """Append original images/videos so they survive the rewrite.

    Skips media whose URL already appears in the rewritten body to avoid
    duplicates.  Checks both the absolute URL (from front-matter) and the
    relativized form (../../media/…) used in markdown.
    """
    imgs = [u for u in _unique(images or [])
            if u not in existing and _relativize_media(u) not in existing][:_MAX_IMG]
    vids = [u for u in _unique(videos or [])
            if not _video_is_embedded(existing, u)][:_MAX_VID]
    parts: list[str] = []
    if imgs:
        parts.extend(f"\n\n![]({_relativize_media(u)})" for u in imgs)
    if vids:
        parts.append("\n\n## 视频（来自原文）\n")
        parts.extend(_video_embed(_relativize_media(u)) for u in vids)
    return "\n".join(parts)


def _interleave_missing_images(body_md: str, images: list[str]) -> str:
    """Distribute images NOT already in *body_md* at section boundaries
    (``## …`` headings) instead of dumping them all at the end.

    Images whose URL (absolute or relativized) already appears in the body
    are skipped to avoid duplicates.  The remaining images are spread across
    natural section breaks in the article, giving a reading flow closer to
    the original layout.  Any overflow past available section boundaries
    falls back to appending at the end.
    """
    images = _unique(images or [])[:_MAX_IMG]
    if not images:
        return body_md

    # Determine which images are already present in the body
    missing: list[str] = []
    for u in images:
        if u not in body_md and _relativize_media(u) not in body_md:
            missing.append(u)
    if not missing:
        return body_md

    # Collect insertion points: blank-line-bounded paragraph breaks plus
    # lines that start a new `##` section heading.
    lines = body_md.split("\n")
    # Prefer section headings — insert ONE image before each `## …` heading
    # (skipping the very first heading to avoid leading-image clutter).
    heading_indices = [
        i for i, ln in enumerate(lines)
        if re.match(r"^## ", ln)
    ]
    # If there aren't enough section headings, also use paragraph breaks
    # (blank lines followed by non-heading text).
    para_break_indices: list[int] = []
    if len(heading_indices) < len(missing):
        for i in range(1, len(lines)):
            if lines[i - 1].strip() == "" and lines[i].strip():
                if i not in heading_indices:
                    para_break_indices.append(i)

    # Build the combined list of insertion positions.
    # Place images BEFORE the heading/paragraph so they visually anchor
    # the following section.
    insert_positions: list[int] = []
    # Use headings first (skipping the very first one)
    for idx in heading_indices[1:]:
        insert_positions.append(idx)
    # Fill remaining slots with paragraph breaks
    need = len(missing) - len(insert_positions)
    if need > 0:
        insert_positions.extend(para_break_indices[:need])

    # Insert images at each position (walk forward, tracking offset so
    # earlier insertions don't shift later positions).
    result_lines = list(lines)
    offset = 0
    for i, pos in enumerate(sorted(set(insert_positions))):
        if i >= len(missing):
            break
        img_url = _relativize_media(missing[i])
        result_lines.insert(pos + offset, f"\n![]({img_url})\n")
        offset += 1

    # Any remaining images go at the very end (original fallback)
    for i in range(len(missing) - offset):
        img_url = _relativize_media(missing[offset + i])
        result_lines.append(f"\n![]({img_url})\n")

    return "\n".join(result_lines)


_CODE_FENCE = re.compile(r'^```(?:\w+)?\s*\n(.*?)\n```\s*$', re.DOTALL)
_CODE_FENCE_INLINE = re.compile(r'```(?:json)?\s*\n(.*?)\n```', re.DOTALL)


def _try_parse_json(text: str) -> dict | None:
    """Parse *text* as JSON with increasingly lenient parsers.

    1. ``json.loads`` — strict spec-compliant parsing.
    2. ``json5.loads`` — handles trailing commas, unescaped control
       characters, single-quoted strings, comments.
    3. ``_repair_json_control_chars`` + ``json.loads`` — escapes bare
       newlines/tabs inside strings for LLM output that violates the spec.
    """
    # 1) Strict JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2) Lenient JSON5 (handles trailing commas, unescaped newlines, etc.)
    try:
        import json5  # type: ignore[import-untyped]
        return json5.loads(text)
    except Exception:
        pass

    # 3) Repair literal control chars, then strict JSON
    repaired = _repair_json_control_chars(text)
    if repaired != text:
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass

    return None


def _strip_code_fences(text: str) -> str | None:
    """If the entire text is wrapped in a markdown code fence, return the
    inner content stripped.  Otherwise return None."""
    m = _CODE_FENCE.match(text.strip())
    return m.group(1).strip() if m else None


def _extract_code_fenced_json(text: str) -> str | None:
    """Find the first json code-fence block and return its inner content."""
    for m in _CODE_FENCE_INLINE.finditer(text):
        inner = m.group(1).strip()
        # The non-greedy regex can accidentally match a closing ``` as
        # an opening fence when code blocks are adjacent; skip these.
        if inner.startswith("```"):
            continue
        if inner.startswith("{") and inner.endswith("}"):
            return inner
    return None


def _repair_json_control_chars(text: str) -> str:
    """Escape unescaped control characters and bare double-quotes inside
    JSON string values so ``json.loads`` can parse LLM output that
    violates the JSON spec."""
    result: list[str] = []
    in_string = False
    escape_next = False
    i = 0
    while i < len(text):
        ch = text[i]
        if escape_next:
            result.append(ch)
            escape_next = False
            i += 1
            continue
        if ch == '\\' and in_string:
            result.append(ch)
            escape_next = True
            i += 1
            continue
        if ch == '"':
            if not in_string:
                # Opening a JSON string
                in_string = True
                result.append(ch)
            else:
                # Closing or content? Check next non-whitespace char.
                # If followed by , } ] or :, it's a structural closing quote.
                # Otherwise it's unescaped content — escape it.
                j = i + 1
                while j < len(text) and text[j] in ' \t\r\n':
                    j += 1
                next_ch = text[j] if j < len(text) else ''
                if next_ch in ',}]:':
                    in_string = False
                    result.append(ch)
                else:
                    # Bare quote inside string — escape it
                    result.append('\\')
                    result.append(ch)
            i += 1
            continue
        if in_string and ch == '\n':
            result.append('\\n')
            i += 1
            continue
        if in_string and ch == '\t':
            result.append('\\t')
            i += 1
            continue
        if in_string and ch == '\r':
            result.append('\\r')
            i += 1
            continue
        result.append(ch)
        i += 1
    return ''.join(result)


def _extract_json_field(text: str, field: str) -> str | None:
    """Regex-based fallback: extract a JSON string field value when the JSON
    itself is malformed (e.g. raw newlines inside string values).  Handles
    ``\\n``, ``\\t``, ``\\r`` and ``\\"`` escape sequences."""
    import re as _re
    pat = _re.compile(
        rf'"{_re.escape(field)}"\s*:\s*"', _re.DOTALL)
    m = pat.search(text)
    if not m:
        return None
    chars: list[str] = []
    i = m.end()
    while i < len(text):
        ch = text[i]
        if ch == '\\' and i + 1 < len(text):
            esc = text[i + 1]
            if esc == 'n':
                chars.append('\n')
            elif esc == 't':
                chars.append('\t')
            elif esc == 'r':
                chars.append('\r')
            elif esc == '"':
                chars.append('"')
            elif esc == '\\':
                chars.append('\\')
            else:
                chars.extend([ch, esc])
            i += 2
        elif ch == '"':
            break
        else:
            chars.append(ch)
            i += 1
    return ''.join(chars)


def _extract_json_fallback(text: str) -> dict | None:
    """When JSON parsing fails, try regex-based extraction of the two
    critical fields: title_candidates and body_md.  This recovers content
    from LLM responses where the body_md contains raw control characters."""
    body_md = _extract_json_field(text, "body_md")
    if body_md is None:
        return None
    # title_candidates is a JSON array — grab the string elements
    import re as _re
    titles: list[str] = []
    tc_m = _re.search(r'"title_candidates"\s*:\s*\[(.*?)\]', text, _re.DOTALL)
    if tc_m:
        for s in _re.findall(r'"([^"]*)"', tc_m.group(1)):
            titles.append(s)
    if not titles:
        return None
    return {"title_candidates": titles, "body_md": body_md}


def _extract_json(text: str) -> dict | None:
    # 1) Direct parse
    result = _try_parse_json(text)
    if result:
        return result

    # 2) Entire text is a single code fence
    inner = _strip_code_fences(text)
    if inner:
        result = _try_parse_json(inner)
        if result:
            return result

    # 3) Find first json code-fence block inside the text
    inner = _extract_code_fenced_json(text)
    if inner:
        result = _try_parse_json(inner)
        if result:
            return result

    # 4) Fallback: find the outermost { … } object
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        result = _try_parse_json(m.group(0))
        if result:
            return result

    return None


def _inline_content(content_md: str, images: list[str],
                    videos: list[str] | None = None) -> str:
    """Replace [[IMG:N]] / [[VIDEO:N]] placeholders from the extractor with
    real markdown / video embeds so the LLM sees them in context and can
    place them naturally."""
    # Safety net: if images exist but the body has no [[IMG:N]] tokens
    # (e.g. legacy archives stored as raw HTML), inject all placeholders
    # so the LLM can still reference them.
    if images and not re.search(r'\[\[IMG:\d+\]\]', content_md):
        placeholder_block = "\n\n".join(
            f"[[IMG:{i}]]" for i in range(len(images)))
        content_md = f"{placeholder_block}\n\n{content_md}"

    for i, url in enumerate(images or []):
        content_md = content_md.replace(
            f"[[IMG:{i}]]", f"\n\n![]({_relativize_media(url)})\n\n")
    for i, url in enumerate(videos or []):
        content_md = content_md.replace(
            f"[[VIDEO:{i}]]",
            f"\n\n{_video_embed(_relativize_media(url))}\n\n")
    return content_md


def render_instruction(template: str, *, title: str, source: str,
                       manifest: str, content: str,
                       seo_block: str = "",
                       promotion_block: str = "") -> str:
    """Render a rewrite instruction *template* by substituting the
    placeholders.  Uses plain string replacement (not ``str.format``) so
    user-authored templates can contain literal ``{`` / ``}`` (e.g. JSON
    examples) without needing to escape them."""
    return (template
            .replace("{manifest}", manifest)
            .replace("{title}", title)
            .replace("{source}", source)
            .replace("{content}", content)
            .replace("{seo_block}", seo_block)
            .replace("{promotion_block}", promotion_block))


def _normalise_tags(md: str) -> str:
    """Normalise tag lines to ``**标签**：#tag1 #tag2`` format.

    - Renames ``**SEO 标签**`` → ``**标签**``
    - Normalises ``、``-separated (Chinese comma) to ``#``-prefixed space-separated
    - Strips spaces within each ``#tag`` word so ``#AI 技术`` → ``#AI技术``
    """
    import re as _re

    def _fix_tag_line(m: _re.Match) -> str:
        line = m.group(0)
        # Replace 、 separation with # prefix
        if "、" in line:
            # **标签**：关键词1、关键词2 → **标签**：#关键词1 #关键词2
            parts = _re.split(r"[、，,]", line)
            head = parts[0]
            # Find the tag prefix start
            before = _re.match(r"^.*?[：:]\s*", parts[0])
            if before:
                rest = line[before.end():]
                tags = [f"#{t.strip()}" for t in _re.split(r"[、，,]", rest)]
                line = before.group() + " ".join(tags)
        # Strip spaces inside each #word
        def _strip_tag(t: _re.Match) -> str:
            return "#" + t.group(1).replace(" ", "")
        line = _re.sub(r"#(\S[^#\s]*)", _strip_tag, line)
        return line

    # Rename **SEO 标签** → **标签**
    md = md.replace("**SEO 标签**", "**标签**")
    # Process tag lines
    return _re.sub(r"^\*\*标签\*\*.*", _fix_tag_line, md, flags=_re.MULTILINE)


def rewrite(article: Article, provider: LLMProvider, style=None,
            promotion_footer: str | None = None,
            seo_tags_enabled: bool = True) -> Draft:
    """Rewrite *article* into a Chinese self-media Draft.

    *style* is an optional ``app.pipeline.styles.RewriteStyle``. When None the
    original 深度科技报道 behaviour is used (backward compatible).

    *promotion_footer* — when provided, tells the LLM to naturally transition
    into this promotional text at the end of the article instead of mechanically
    appending it post-rewrite.

    *seo_tags_enabled* — generate a concise SEO keyword line immediately before
    the promotional section.
    """
    images = article.images or []
    videos = getattr(article, "videos", []) or []

    # Build manifest so LLM knows which media tokens it can use
    manifest, media_map = _build_manifest(images, videos)

    # Pre-expand [[IMG:N]] / [[VIDEO:N]] tokens to real markdown images so
    # the LLM sees them IN CONTEXT at their original positions — this is much
    # more reliable than asking the LLM to manually reproduce [[IMG:N]] tokens.
    body_raw = article.content_md or ""
    body = _inline_content(body_raw, images, videos)

    # Ensure the visible body is long enough that images embedded by
    # _inline_content are meaningful (no strict slice — we want the full
    # article context with images inline).
    img_end = max(
        [body.find(_relativize_media(u)) for u in images
         if body.find(_relativize_media(u)) >= 0]
        or [body.find(_relativize_media(v)) for v in videos
            if body.find(_relativize_media(v)) >= 0]
        or [0])
    slice_limit = max(8000, img_end + 500)
    content = body[:slice_limit]

    if style is None:
        system_prompt = REWRITE_SYSTEM
        # Convert the str.format template ({{/}} escaped) to replace-form.
        instruction_tmpl = REWRITE_INSTRUCTION.replace("{{", "{").replace("}}", "}")
    else:
        system_prompt = style.prompt + "\n\n" + ANTI_SLOP_SYSTEM_INSTRUCTION
        instruction_tmpl = style.instruction

    # Older custom styles predate the SEO placeholder. Inject it immediately
    # before their promotion placeholder (or at the end) so the global setting
    # applies consistently without requiring users to edit every custom style.
    if "{seo_block}" not in instruction_tmpl:
        if "{promotion_block}" in instruction_tmpl:
            instruction_tmpl = instruction_tmpl.replace(
                "{promotion_block}", "{seo_block}\n{promotion_block}")
        else:
            instruction_tmpl = f"{instruction_tmpl}\n\n{{seo_block}}"

    seo_block = (
        "4. **标签**：在此处单独输出一行文章标签。\n"
        "   - 格式固定为：`**标签**：#关键词1 #关键词2 #关键词3`\n"
        "   - 每个标签以 # 开头，标签词内不要有空格\n"
        "   - 从本文标题和正文提取 5-8 个具体关键词，包含核心主题、"
        "关键实体和行业术语\n"
        "   - 不得使用正文未提及的词，不要写成句子，不要添加解释\n"
        if seo_tags_enabled else ""
    )

    # Promotion block: if provided, tells the LLM to generate a
    # context-aware promotional call-to-action based on the article's
    # actual topic/content, using the configured footer as a reference
    # for the brand name and CTA format — NOT as verbatim content.
    promotion_block = (
        f"{'5' if seo_tags_enabled else '4'}. **推广部分**："
        f"写一段与本文主题相关的推广引导，包含：\n"
        f"   - 一句自然的引导语（如「如果觉得有收获」）\n"
        f"   - 2-3 个互动呼吁（从点赞、关注、评论、收藏中选），"
        f"每个的文案必须基于本文实际内容，不得照抄下文的示例\n"
        f"   - 末尾标注品牌名称\n\n"
        f"参考格式（提取其中的品牌名称和结构，但把具体主题内容替换为与本文匹配的）：\n"
        f"{promotion_footer}\n"
        if promotion_footer else ""
    )
    rewrite_prompt = render_instruction(
        instruction_tmpl, title=article.title, source=article.source_name,
        manifest=manifest, content=content,
        seo_block=seo_block,
        promotion_block=promotion_block)
    raw = provider.chat([
        Message(role="system", content=system_prompt),
        Message(role="user", content=rewrite_prompt),
    ])
    parsed = _extract_json(raw)
    if parsed and parsed.get("title_candidates") and parsed.get("body_md"):
        title_candidates = parsed["title_candidates"]
        body_md = parsed["body_md"]
    else:
        # JSON parse failed — try regex-based field extraction as fallback
        fallback = _extract_json_fallback(raw)
        if fallback:
            title_candidates = fallback["title_candidates"]
            body_md = fallback["body_md"]
        else:
            # Retry once with a stricter prompt that forbids meta-commentary
            retry_prompt = (
                rewrite_prompt + "\n\n"
                "⚠️ 你的上一次回复没有包含有效的 JSON 格式输出。"
                "请严格按照要求，只输出一个 JSON 对象（以 { 开始，以 } 结束），"
                "不要添加任何前言、后语、解释、摘要、「已完成」等元评论。"
            )
            raw2 = provider.chat([
                Message(role="system", content=system_prompt),
                Message(role="user", content=retry_prompt),
            ])
            parsed2 = _extract_json(raw2)
            if parsed2 and parsed2.get("title_candidates") and parsed2.get("body_md"):
                title_candidates = parsed2["title_candidates"]
                body_md = parsed2["body_md"]
                raw = raw2  # use retried response for fact-check
            else:
                fb2 = _extract_json_fallback(raw2)
                if fb2:
                    title_candidates = fb2["title_candidates"]
                    body_md = fb2["body_md"]
                    raw = raw2
                else:
                    title_candidates = [article.title]
                    body_md = raw

    # Post-process: remove AI slop patterns from the LLM output
    body_md = post_process(body_md)

    # Normalise tag format: strip spaces within #tag words so multi-word
    # tags like "AI 技术" become "AI技术"
    body_md = _normalise_tags(body_md)

    # Replace any stray [[IMG:N]] / [[VID:N]] tokens the LLM may have output
    body_md = _apply_placeholders(body_md, media_map)

    # Repair a common LLM mistake: Markdown image syntax cannot represent
    # videos. Convert known video URLs to iframe + fallback-link embeds.
    body_md = _normalize_video_markdown(body_md, videos)

    # Distribute images the LLM didn't place inline across section boundaries
    # (instead of dumping them all at the end).
    body_md = _interleave_missing_images(body_md, images)

    # Append any remaining (unused) videos at the end so nothing is lost
    vid_block = _media_block([], videos, body_md)
    body_with_source = (
        f"{body_md}{vid_block}\n\n---\n**信息来源**："
        f"[{article.source_name}]({article.url})\n"
    )

    # Fact-check on the PLAIN rewritten text (use body_raw without inlined
    # image URLs so the fact-checker compares text against text).
    check_content = body_raw[:slice_limit]
    check_prompt = CHECK_INSTRUCTION.format(
        source_content=check_content, draft=body_md)
    check_raw = provider.chat([Message(role="user", content=check_prompt)])
    check_parsed = _extract_json(check_raw) or {}
    flagged = check_parsed.get("flagged_claims", []) or []

    return Draft(
        article_id=article.id or 0,
        title_candidates=title_candidates,
        body_md=body_with_source,
        topic=article.topic or "uncategorized",
        source_url=article.url,
        source_name=article.source_name,
        cover_image=None,
        flagged_claims=flagged,
    )
