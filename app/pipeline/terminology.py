"""术语该留英文还是写中文：给模型的规则，加上对成稿的检查。

这件事栽过三次，每次都是同一种错——把规则写成了一边倒。

一、一概译成中文，于是 AI agent 被写成「AI 代理」。
二、改成「专业术语一律保留英文」，training、latency、open source 这些早有通行
    中文说法的词也留在句子中间，读起来像没译完。
三、改成按词判断、两个方向都说，仍旧漏了短语：规则按「词」写，模型按「短语」
    用。prompt 在留英文的名单里，于是 plain-language prompt 整个短语原封不动
    留在中文句子里——plain-language 是普通英文，该写成「大白话」。

所以规则现在写死两条：英文只留术语本身那一个词，修饰它、连接它的一律中文；
拿不准的时候默认译。规则说不清的部分交给成对的改法示例。

前两次都是靠提示词硬扛，写坏了没人发现，全靠读者回头抱怨。所以这里还提供
:func:`check_terms`：把成稿里剩下的英文短语和该译没译的词机械地扫一遍报出来。
词表两边共用，免得提示词里说要译、检查却不认，或者反过来。
"""

from __future__ import annotations

import re

from app.pipeline import references

# ── 词表：规则和检查共用 ────────────────────────────────────────────────
# 下面两份词表都只是举例——换个主题就换一批词，这种表永远列不全，所以它
# 不负责决定报不报，只负责在报的时候顺手给出中文说法。真正拦人的是
# :func:`_stays_english` 那套结构判断：该留英文的集合才是封闭的（名字、缩写，
# 加上中文圈确实照写的那几个小写词），剩下的英文一概先当没译完看。
#
# 早有通行中文说法的术语，留着英文就是没译完。第二次栽的就是这批词。
MUST_TRANSLATE: tuple[tuple[str, str], ...] = (
    ("training", "训练"),
    ("inference", "推理"),
    ("fine-tuning", "微调"),
    ("dataset", "数据集"),
    ("parameters", "参数"),
    ("weights", "权重"),
    ("latency", "延迟"),
    ("throughput", "吞吐"),
    ("open-source", "开源"),
    ("deployment", "部署"),
    ("quantization", "量化"),
    ("distillation", "蒸馏"),
    ("multimodal", "多模态"),
    ("benchmark", "基准测试"),
    ("accuracy", "准确率"),
)

# 普通词汇根本不是术语，留着英文没有任何理由。
PLAIN_WORDS: tuple[tuple[str, str], ...] = (
    ("release", "发布"),
    ("performance", "性能"),
    ("significant", "显著"),
    ("feature", "功能"),
    ("update", "更新"),
)

# 中文技术圈确实照写的小写英文词。名字和缩写不用列——大写就认得出来，
# 而这一份是真的短：中文里说「agent」「prompt」「token」不别扭，说
# 「training」「latency」就是没译完。列不准顶多多报一条让人自己看，
# 这份名单只要错在「多报」那一边就是安全的。
KEEP_WORDS: frozenset[str] = frozenset({
    "agent", "agents", "prompt", "prompts", "token", "tokens",
    "embedding", "embeddings", "app", "bug", "bugs",
})

# 中文技术圈确实整段照写的英文短语。同样只列几个。
KEPT_PHRASES: frozenset[str] = frozenset({
    "system prompt",
    "prompt injection",
})

# 改法示例。规则讲不清「哪半截该译」，示例一看就懂；最后两条是反向的——
# 该留的也别乱动，免得又矫正到另一边去。
_EXAMPLES: tuple[tuple[str, str, str], ...] = (
    ("plain-language prompt", "大白话 prompt",
     "plain-language 是普通英文，只有 prompt 是术语"),
    ("inference latency", "推理延迟", "两个词都有通行中文说法"),
    ("long-context model", "长上下文模型", "整个短语都译"),
    ("agentic workflow", "agent 工作流", "agent 留着，workflow 要译"),
    ("state-of-the-art results", "当前最好的结果", "普通英文，别留"),
    ("human-in-the-loop review", "人工复核", "照字面译反而没人看懂"),
    ("GPT-4 Turbo", "GPT-4 Turbo", "产品名，原样不动"),
    ("system prompt", "system prompt", "中文圈就这么写，属于少数整段留英文的"),
)


def _pairs(items: tuple[tuple[str, str], ...]) -> str:
    return "、".join(f"{en} 写「{zh}」" for en, zh in items)


def _example_table() -> str:
    rows = "".join(f"| {bad} | {good} | {why} |\n" for bad, good, why in _EXAMPLES)
    return ("| 原文里的写法 | 该写成 | 为什么 |\n|---|---|---|\n" + rows)


TERM_RULE = (
    "### 术语的中英文\n\n"
    "像专业译者那样逐处判断：该译的译，不该译的不动。判断标准只有一条——"
    "中文技术圈平时写这个词，写的是中文还是英文？跟着他们写。"
    "拿不准就译成中文：留英文是例外，得说得出理由（是名字、是缩写、"
    "中文圈确实这么写）。\n"
    "- **留英文**：模型名、产品名、公司名（GPT-4、Claude、Llama）；缩写"
    "（LLM、RAG、MoE、API、SDK、GPU）；中文圈本来就直接说英文的词"
    "（agent、prompt、token、Transformer）。把 AI agent 写成「AI 代理」"
    "反而显得外行。\n"
    "- **用中文**：已经有通行中文说法的一律译过来，例如"
    + _pairs(MUST_TRANSLATE) + "。这几个只是举例——凡是中文里有说法的都算，"
    "不在这份例子里不等于可以留着英文。这类词留着英文不叫专业，叫没译完。\n"
    "- **普通词汇不是术语**，一律用中文，例如"
    + _pairs(PLAIN_WORDS) + "，同样只是举例。\n"
    "- **英文只留术语本身那一个词**，修饰它的、连接它的、跟它并列的普通英文"
    "全都要写成中文。一个术语在留英文的名单里，不等于带着它的整个短语都能"
    "留在英文里——这是最容易出错的地方。除了产品名（GPT-4 Turbo）和固定"
    "缩写，正文里连着两个以上的英文单词，基本都是没译完。\n"
    "- 译成中文的术语，第一次出现时可以在括号里标一下英文原词，例如"
    "「上下文窗口（context window）」，后文直接用中文。\n"
    "- 读者可能不认识的术语，第一次出现时用一句话说清它是什么、解决什么问题，"
    "再往下用。这句解释跟留不留英文是两件事，别拿「保留英文」当成不用解释。\n\n"
    + _example_table() + "\n"
    "写完自查一遍：正文里每一处英文都问一句「中文技术圈会这么写吗」。"
    "凡是连着两个以上英文单词、又不是产品名的地方，都改成中文。"
)

# 系统提示词和速查表格里塞不下整段规则，用这一句，完整规则由指令那边给。
TERM_RULE_LINE = (
    "术语按中文技术圈的习惯写：模型名、缩写和 agent、prompt 这类词留英文原文，"
    "已有通行中文说法的（训练、推理、微调、延迟、开源…）一律译成中文，"
    "普通词汇一律用中文；英文只留术语本身那一个词，修饰它的词要写成中文"
    "（plain-language prompt 写成「大白话 prompt」），拿不准就译。"
)

# ── 对成稿的检查 ────────────────────────────────────────────────────────

_FENCE = re.compile(r"^\s*(?:```|~~~)")
# ::: 是本项目的组件语法，不是正文。
_DIRECTIVE = re.compile(r"^\s*:::")
# 引文和出处行按规矩就该留着英文原文，不能算没译。
_QUOTE = re.compile(r"^\s*>")
_ATTRIBUTION = re.compile(r"(原文|来源|出处|译自|参考|Source)\s*[:：]")
_URL = re.compile(r"https?://\S+")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_TARGET = re.compile(r"\]\([^)]*\)")
_INLINE_CODE = re.compile(r"`[^`]*`")
_HTML_TAG = re.compile(r"<[^>]{1,200}>")
# 「上下文窗口（context window）」——括号里标一下英文原词是规则自己建议的写法，
# 不能反手报它没译。只认整段都是英文的括号，中文夹在里面的照查。
_GLOSS = re.compile(r"[（(][ A-Za-z0-9.,'’/&+-]+[）)]")

# 只认 ASCII 字母：\w 在 Python 里连中文一起吃，用它就永远命中整句话。
_WORD_CHARS = r"[A-Za-z0-9]"
_WORD_TAIL = r"[A-Za-z0-9.'’-]*"
_WORD = re.compile(rf"[A-Za-z]{_WORD_TAIL}")
_RUN = re.compile(
    rf"[A-Za-z]{_WORD_TAIL}(?:[ ]+{_WORD_CHARS}{_WORD_TAIL})+")
# 大写开头的短语里夹着这些小词不影响它是个名字：Mixture of Experts、
# Attention Is All You Need。
_FUNCTION_WORDS = frozenset({
    "a", "an", "and", "as", "at", "by", "for", "in", "is", "of", "on",
    "or", "the", "to", "vs", "with",
})

_MUST = {en: zh for en, zh in MUST_TRANSLATE + PLAIN_WORDS}

LABELS = {"phrase": "英文短语没译", "word": "英文词没译"}

_PHRASE_HINT = "英文只留术语本身，修饰它的词要写成中文"
_WORD_HINT = "不是名字也不是缩写，中文技术圈有说法就译过来"


def _prose_lines(text: str) -> list[tuple[int, str]]:
    """正文里真正是「话」的那些行，带原始行号。

    代码块、组件标记、图片、链接地址里的英文是给机器看的；引文和「原文：」
    这类出处行按规矩本来就留英文原文。这些地方留着英文都不算没译，扫进来只会
    让每篇稿子都背上一堆没法改的账。文末参考文献同理，整段摘掉。
    """
    out: list[tuple[int, str]] = []
    in_fence = False
    for index, raw in enumerate(references.split(text or "")[0].splitlines()):
        if _FENCE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence or _DIRECTIVE.match(raw) or _QUOTE.match(raw):
            continue
        if _ATTRIBUTION.search(raw):
            continue
        line = _IMAGE.sub(" ", raw)
        line = _LINK_TARGET.sub(" ", line)
        line = _URL.sub(" ", line)
        line = _INLINE_CODE.sub(" ", line)
        line = _GLOSS.sub(" ", line)
        out.append((index + 1, _HTML_TAG.sub(" ", line)))
    return out


def _trim(phrase: str) -> str:
    return phrase.strip(" .'’-")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _is_name(phrase: str) -> bool:
    """整串都是名字/缩写吗（GPT-4 Turbo、Hugging Face、Mixture of Experts）。"""
    words = phrase.split()
    return all(word[:1].isupper() or word[:1].isdigit()
               or word.lower() in _FUNCTION_WORDS
               for word in words)


def _is_unit(line: str, start: int) -> bool:
    """紧跟在数字后面的是单位，不是词：128k、200ms、7b。"""
    return start > 0 and line[start - 1].isdigit()


def _stays_english(word: str) -> bool:
    """这个词留着英文是对的吗？

    判断反过来做：不去列「该译的词」——换个主题就换一批，那种表永远列不全，
    上一版就是被这个卡住的。该留英文的集合才是封闭的：名字和缩写靠大小写
    认（GPT-4、LLM、Claude、Transformer），中文圈确实照写的小写词只有
    :data:`KEEP_WORDS` 那几个。剩下的一概先当没译完。
    """
    if word[:1].isupper() or word.isupper() or any(c.isdigit() for c in word):
        return True
    return word.lower().strip(".'’-") in KEEP_WORDS


def _chinese_for(word: str) -> str | None:
    """这个词的通行中文说法，词表里有就给出来。顺手认一下复数。"""
    key = word.lower()
    if key in _MUST:
        return _MUST[key]
    if key.endswith("s") and len(key) > 3 and key[:-1] in _MUST:
        return _MUST[key[:-1]]
    return _MUST.get(key + "s")


def _finding(kind: str, text: str, line: int, hint: str) -> dict:
    return {"kind": kind, "label": LABELS[kind],
            "text": text[:120], "line": line, "hint": hint}


def term_samples(findings: list[dict], limit: int = 3) -> str:
    """报给人看的头几处。一篇稿子命中几十处时，进度条上列全了没人读。"""
    seen = list(dict.fromkeys(f["text"] for f in findings))
    shown = "、".join(seen[:limit])
    return f"{shown}…" if len(seen) > limit else shown


def check_terms(text: str) -> list[dict]:
    """挑出成稿里没译完的英文，按行号排好。

    两种命中：连着两个以上英文单词的短语（名字和 :data:`KEPT_PHRASES` 除外），
    以及单个留着英文、但按 :func:`_stays_english` 判下来没理由留的词。判断不
    依赖任何主题词表——词表只在报的时候顺手给出中文说法，列不全也不影响谁被
    报出来。

    只报不改——「该不该留英文」终究要人看一眼，机器判死了迟早把该留的也给
    译了。
    """
    findings: list[dict] = []
    for line_no, line in _prose_lines(text):
        covered: list[tuple[int, int]] = []
        for match in _RUN.finditer(line):
            phrase = _trim(match.group(0))
            if not phrase or " " not in phrase:
                continue
            covered.append(match.span())
            if _norm(phrase) in KEPT_PHRASES or _is_name(phrase):
                continue
            findings.append(
                _finding("phrase", phrase, line_no, _PHRASE_HINT))
        for match in _WORD.finditer(line):
            # 短语已经报过了，同一处不再按单词报一遍。
            if any(start <= match.start() and match.end() <= end
                   for start, end in covered):
                continue
            word = _trim(match.group(0))
            # 单个字母是标号、公式和单位的碎渣，报出来没人看得懂要改什么。
            if len(word) < 2 or _is_unit(line, match.start()):
                continue
            if _stays_english(word):
                continue
            chinese = _chinese_for(word)
            findings.append(_finding(
                "word", match.group(0), line_no,
                f"写「{chinese}」" if chinese else _WORD_HINT))
    findings.sort(key=lambda f: f["line"])
    return findings
