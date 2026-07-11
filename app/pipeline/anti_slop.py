"""Anti-AI-slop system for rewrite styles.

Provides two layers of protection:

1. **System-prompt injection** — ``ANTI_SLOP_SYSTEM_INSTRUCTION`` is appended to
   every style's system prompt so the LLM avoids slop in the first place.

2. **Post-processing** — :func:`post_process` runs regex-based cleanup on the
   LLM's output as a safety net for patterns the LLM might still produce.

Usage::

    from app.pipeline.anti_slop import ANTI_SLOP_SYSTEM_INSTRUCTION, post_process

    # Layer 1: append to system prompt
    system_prompt = style.prompt + "\\n\\n" + ANTI_SLOP_SYSTEM_INSTRUCTION

    # Layer 2: clean up LLM output
    body_md = post_process(body_md)
"""

from __future__ import annotations

import re
from typing import Pattern

# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — system-prompt injection (appended to every style's system prompt)
# ─────────────────────────────────────────────────────────────────────────────

ANTI_SLOP_SYSTEM_INSTRUCTION: str = (
    "### 严禁 AI 腔（必须遵守）\n\n"
    "你写的每一句话都必须通过「正常人会这样说吗？」测试。\n"
    "以下规则是死命令，必须逐条遵守：\n\n"
    "1. **忌空泛开头** — 不要以「随着」「近年来」「在当今」「在……背景下」"
    "「在……的浪潮中」开头。直接切入正题。\n"
    "2. **忌假深刻** — 不要用「不仅是……更是……」「表面上……本质上……」"
    "「某种程度上」这类套路句式。\n"
    "3. **忌报告套话** — 绝对不要出现以下词汇："
    "「扎实推进」「形成闭环」「赋能」「抓手」「沉淀」「对齐」「颗粒度」"
    "「底层逻辑」「顶层设计」「落地」（作为抽象名词时）。\n"
    "4. **忌科技博客体** — 绝对不要出现以下词汇："
    "「深入浅出」「最佳实践」「干货满满」「保姆级教程」「一站式」"
    "「不容错过」「值得收藏」。\n"
    "5. **忌公众号体** — 不要写「愿我们都能……」「这才是真正的……」"
    "「X 是最好的 X」「扎心了」。\n"
    "6. **忌假口语** — 不要写「说实话」「你会发现」「其实很简单」"
    "「懂的都懂」「你品，你细品」「懂的自然懂」。\n"
    "7. **忌机械结构** — 不要用「三个方面」「三大优势」「以下几点」"
    "「总的来说」「综上所述」硬凑段落。\n"
    "8. **忌结尾升华** — 不要在结尾写「在……的道路上，我们……」"
    "「愿我们都能……」这类空洞升华。\n"
    "9. **忌堆砌修辞** — 不要一句话里塞两个以上的形容词或排比。\n\n"
    "黄金法则：如果一段话去掉后不影响事实表达，删掉它。"
)


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — post-processing patterns (regex cleanup of LLM output)
# ─────────────────────────────────────────────────────────────────────────────

# Each entry is (compiled_regex, replacement_string).
# Order matters — run more specific patterns first.
_SLOP_PATTERNS: list[tuple[Pattern[str], str]] = [
    # ── Opening slop ────────────────────────────────────────────────────
    # "随着...的XX" at beginning of text (catch "的发展", "的快速发展", "的突破", etc.)
    (
        re.compile(r"^随着[^。，\n]{0,40}的[^。，\n，]{0,10}[，,。]?", re.MULTILINE),
        "",
    ),
    # "近年来" at beginning
    (re.compile(r"^近年来[，,。]?", re.MULTILINE), ""),
    # "在当今" / "在当今时代" at beginning
    (re.compile(r"^在当今[^。，\n]{0,20}[，,。]?", re.MULTILINE), ""),
    # "在...的浪潮中"
    (re.compile(r"在[^。，\n]{1,30}的浪潮中[，,。]?", re.MULTILINE), ""),
    # "在...背景下"
    (re.compile(r"在[^。，\n]{1,30}背景下[，,。]?", re.MULTILINE), ""),
    # "随着..." (not at beginning — mid-paragraph)
    (re.compile(r"随着[^。，\n]{0,40}的[^。，\n，]{0,10}[，,。]?", re.MULTILINE), ""),

    # ── Fake depth ──────────────────────────────────────────────────────
    (re.compile(r"不仅是[^，,。\n]{1,60}，更是[^，,。\n]{1,60}[，。]?", re.MULTILINE), ""),
    (re.compile(r"表面上[^，,。\n]{1,40}，本质上[^，,。\n]{1,40}[，。]?", re.MULTILINE), ""),
    (re.compile(r"某种意义上[^，。\n]{0,20}[，。]?", re.MULTILINE), ""),

    # ── Report jargon ───────────────────────────────────────────────────
    (re.compile(r"扎实推进"), ""),
    (re.compile(r"形成闭环"), ""),
    (re.compile(r"赋能(?!.*[的给为于被])"), ""),  # "赋能" as standalone
    (re.compile(r"(?:赋能|抓手|沉淀|对齐|颗粒度|底层逻辑|顶层设计)\s*[，。\n]?"), ""),

    # ── Tech blog clichés ───────────────────────────────────────────────
    (re.compile(r"深入浅出"), ""),
    (re.compile(r"最佳实践"), ""),
    (re.compile(r"干货满满"), ""),
    (re.compile(r"保姆级教程"), ""),
    (re.compile(r"一站式[^。\n]{0,20}"), ""),
    (re.compile(r"不容错过"), ""),
    (re.compile(r"值得收藏"), ""),

    # ── WeChat public account style ─────────────────────────────────────
    (re.compile(r"愿我们都能[^。\n]{0,60}[。\n]?"), ""),
    (re.compile(r"这才是真正的[^。\n]{0,60}[。\n]?"), ""),

    # ── Fake spoken language ────────────────────────────────────────────
    (re.compile(r"(?:说实话|说实话，|说实话、)"), ""),
    (re.compile(r"(?:你会发现，|你会发现 )"), ""),
    (re.compile(r"(?:其实很简单[，。]?)"), ""),
    (re.compile(r"(?:懂的都懂|懂的自然懂|你品，你细品|你品)"), ""),

    # ── Mechanical structure ────────────────────────────────────────────
    (re.compile(r"(?:总的来说|综上所述)\s*[，,。]?"), ""),

    # ── Ending elevation ───────────────────────────────────────────────
    (re.compile(r"在[^。，\n]{1,40}的道路上，我们[^。\n]{0,60}[。\n]?"), ""),
]


def post_process(body_md: str) -> str:
    """Remove common AI slop patterns from rewritten text.

    This is a conservative safety net — it removes obviously bad phrases
    without attempting major rewrites.  The heavy lifting is done by
    the system-prompt injection (Layer 1).

    Returns the cleaned markdown string.
    """
    if not body_md:
        return body_md

    cleaned = body_md
    for pattern, replacement in _SLOP_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)

    # Clean up: remove double blank lines left by removed text
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    # Clean up: leading blank lines
    cleaned = cleaned.lstrip("\n")
    return cleaned
