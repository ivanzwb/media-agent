from __future__ import annotations

import json
import re

from app.llm.base import LLMProvider, Message
from app.models import Article, Draft

REWRITE_SYSTEM = (
    "你是科技媒体主编，负责将英文技术文章改写为深度中文自媒体报道。"
    "改写后的文章应像《知日》《极客公园》《硅星人》等一线科技媒体的作品。"
    "基于且仅基于提供的原文事实进行改写——绝不可编造原文没有的数据、引用、结论或时间。"
    "目标：用最通俗的语言讲最硬的科技，让非技术读者也能看懂。"
    "绝不在原文基础上添加夸张描述或自己的判断。"
    "不确定的内容不要写。"
)

REWRITE_INSTRUCTION = (
    "分两步完成：\n\n"
    "**第一步 — 完整理解**：通读全文，确保理解所有技术细节、数据、结论和引用。\n\n"
    "**第二步 — 深度科技报道转写**：在理解的基础上，写成中文深度报道。\n\n"
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
    "3. **原文信息**：注明原文标题、来源和日期\n\n"
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
    "- 引用原文一定要准确对应，不要混淆不同来源的信息\n\n"
    "### 图文结合\n\n"
    "- 原文正文中的图片（![]()）与视频（<iframe>）已嵌入对应段落之间\n"
    "- 改写时保留最相关的 **2-4 张** 图片，放在正文中最合适的位置\n"
    "- 如果有原文视频（<iframe>），也必须放在正文中与内容相关的合适位置，不要堆在开头或结尾\n"
    "- 每张图/每个视频要与附近文字内容相关\n"
    "- 如果某张图/视频跟内容不相关就删掉，不要硬塞\n\n"
    "输出严格的 JSON，字段：\n"
    '  "title_candidates": [3 个吸睛但不虚假的标题],\n'
    '  "body_md": "Markdown 正文（中文数字编号章节+短段落+'
    '图片用 ![]() 语法、视频用 <iframe> 语法在相关段落间自然嵌入）"\n'
    "不要输出 JSON 以外的任何内容。\n\n"
    "原文标题：{title}\n来源：{source}\n\n原文正文：\n{content}"
)

CHECK_INSTRUCTION = (
    "你是事实校验员。对照原文，找出改写稿中原文未提及或可能失真的具体说法。\n"
    "输出严格 JSON：{{\"flagged_claims\": [可疑说法的简短描述]}}。"
    "若全部忠实于原文，返回空数组。不要输出 JSON 以外内容。\n\n"
    "原文：\n{source_content}\n\n改写稿：\n{draft}"
)


_MAX_IMG = 12
_MAX_VID = 8
_PLACEHOLDER_RE = re.compile(r"\[{1,2}\s*(IMG|VID)\s*(\d+)\s*\]{1,2}", re.I)


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
    return (f'<iframe src="{url}" width="100%" height="420" '
            'frameborder="0" allowfullscreen></iframe>\n'
            f'[▶ 视频链接]({url})')


def _build_manifest(images: list[str], videos: list[str]):
    """Return (manifest_text, media_map) for placeholder-based insertion.

    media_map maps a token like "IMG1" -> ("img"|"vid", url).
    """
    imgs = _unique(images or [])[:_MAX_IMG]
    vids = _unique(videos or [])[:_MAX_VID]
    media_map: dict[str, tuple[str, str]] = {}
    lines: list[str] = []
    if imgs:
        lines.append(
            "【可用配图】请在 body_md 中与内容相关的段落之间插入下列占位符"
            "（每个独占一行，最多用一次，内容不相关就不要插入）：")
        for i, u in enumerate(imgs, 1):
            tok = f"IMG{i}"
            media_map[tok] = ("img", u)
            lines.append(f"[[{tok}]] - {_hint(u)}")
    if vids:
        lines.append("【可用视频】同理用下列占位符：")
        for i, u in enumerate(vids, 1):
            tok = f"VID{i}"
            media_map[tok] = ("vid", u)
            lines.append(f"[[{tok}]] - {_hint(u)}")
    if media_map:
        lines.append(
            "规则：每张图片都必须在正文最相关的位置插入占位符（如 [[IMG1]]），"
            "不要编造不存在的图片/视频，不要把所有图片堆在文末。")
    return "\n".join(lines), media_map


def _apply_placeholders(body: str, media_map: dict[str, tuple[str, str]]) -> str:
    """Replace [[IMG1]]/[[VID1]] placeholders with real markdown/embeds.

    Unknown or leftover placeholders are removed.
    """
    def repl(m: re.Match) -> str:
        tok = f"{m.group(1).upper()}{m.group(2)}"
        item = media_map.get(tok)
        if not item:
            return ""
        kind, url = item
        if kind == "img":
            return f"\n\n![]({url})\n\n"
        return f"\n\n{_video_embed(url)}\n\n"

    return _PLACEHOLDER_RE.sub(repl, body)


def _media_block(images: list[str], videos: list[str], existing: str) -> str:
    """Append original images/videos so they survive the rewrite.

    Skips media whose URL already appears in the rewritten body to avoid
    duplicates.
    """
    imgs = [u for u in _unique(images or []) if u not in existing][:_MAX_IMG]
    vids = [u for u in _unique(videos or []) if u not in existing][:_MAX_VID]
    parts: list[str] = []
    if imgs:
        parts.extend(f"\n\n![]({_relativize_media(u)})" for u in imgs)
    if vids:
        parts.append("\n\n## 视频（来自原文）\n")
        parts.extend(_video_embed(_relativize_media(u)) for u in vids)
    return "\n".join(parts)


_CODE_FENCE = re.compile(r'^```(?:\w+)?\s*\n(.*?)\n```\s*$', re.DOTALL)
_CODE_FENCE_INLINE = re.compile(r'```(?:json)?\s*\n(.*?)\n```', re.DOTALL)


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
    """Escape unescaped control characters (newlines, tabs) inside JSON
    string values so ``json.loads`` can parse LLM output that violates
    the JSON spec."""
    result: list[str] = []
    in_string = False
    escape_next = False
    for ch in text:
        if escape_next:
            result.append(ch)
            escape_next = False
            continue
        if ch == '\\' and in_string:
            result.append(ch)
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if in_string and ch == '\n':
            result.append('\\n')
            continue
        if in_string and ch == '\t':
            result.append('\\t')
            continue
        if in_string and ch == '\r':
            result.append('\\r')
            continue
        result.append(ch)
    return ''.join(result)


def _extract_json(text: str) -> dict | None:
    # 1) Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2) Entire text is a single code fence
    inner = _strip_code_fences(text)
    if inner:
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            pass
        repaired = _repair_json_control_chars(inner)
        if repaired != inner:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass

    # 3) Find first json code-fence block inside the text
    inner = _extract_code_fenced_json(text)
    if inner:
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            pass
        repaired = _repair_json_control_chars(inner)
        if repaired != inner:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass

    # 4) Fallback: find the outermost { … } object
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        obj = m.group(0)
        try:
            return json.loads(obj)
        except json.JSONDecodeError:
            pass
        repaired = _repair_json_control_chars(obj)
        if repaired != obj:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass

    return None


def _inline_content(content_md: str, images: list[str],
                    videos: list[str] | None = None) -> str:
    """Replace [[IMG:N]] / [[VIDEO:N]] placeholders from the extractor with
    real markdown / video embeds so the LLM sees them in context and can
    place them naturally."""
    for i, url in enumerate(images or []):
        content_md = content_md.replace(
            f"[[IMG:{i}]]", f"\n\n![]({_relativize_media(url)})\n\n")
    for i, url in enumerate(videos or []):
        content_md = content_md.replace(
            f"[[VIDEO:{i}]]",
            f"\n\n{_video_embed(_relativize_media(url))}\n\n")
    return content_md


def rewrite(article: Article, provider: LLMProvider) -> Draft:
    videos = getattr(article, "videos", []) or []

    # Replace [[IMG:N]] / [[VIDEO:N]] with real media so the LLM sees them inline
    content = _inline_content(article.content_md[:6000],
                              article.images or [], videos)

    rewrite_prompt = REWRITE_INSTRUCTION.format(
        title=article.title, source=article.source_name,
        content=content)
    raw = provider.chat([
        Message(role="system", content=REWRITE_SYSTEM),
        Message(role="user", content=rewrite_prompt),
    ])
    parsed = _extract_json(raw)
    if parsed and parsed.get("title_candidates") and parsed.get("body_md"):
        title_candidates = parsed["title_candidates"]
        body_md = parsed["body_md"]
    else:
        title_candidates = [article.title]
        body_md = raw

    # Append any remaining (unused) media at the end so nothing is lost
    media = _media_block(article.images, videos, body_md)
    body_with_source = (
        f"{body_md}{media}\n\n---\n**信息来源**："
        f"[{article.source_name}]({article.url})\n"
    )

    # Fact-check on the plain rewritten text
    check_prompt = CHECK_INSTRUCTION.format(
        source_content=content, draft=body_md)
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
