from __future__ import annotations

import re
from functools import lru_cache

# Sensitive / restricted words that commonly trigger throttling or rejection on
# Chinese self-media platforms. Focused on 广告法绝对化用语 / 夸大宣传 / 医疗金融
# 夸大 / 引流导流 — intentionally non-political and conservative. Extend via the
# user's custom list in settings.

_ADLAW = {
    "最佳", "最好", "最大", "最高", "最强", "最先进", "最优", "最低价",
    "第一", "全球第一", "全国第一", "首选", "首个", "顶级", "顶尖",
    "国家级", "世界级", "最高级", "史上最", "绝无仅有", "独一无二",
    "100%", "百分百", "全网最低", "限时抢购", "万能",
}
_MEDICAL = {
    "治愈", "包治百病", "药到病除", "抗癌", "壮阳", "丰胸", "减肥神器",
    "特效药", "根治", "无毒副作用", "纯天然无添加",
}
_FINANCE = {
    "稳赚", "保本", "稳赚不赔", "一夜暴富", "暴富", "必涨", "稳定盈利",
    "内幕消息", "无风险", "高额回报",
}
_MARKETING = {
    "加微信", "加微", "扫码", "扫一扫", "点击链接", "私信我", "私我",
    "代购", "免费领取", "点击购买", "vx", "v信",
}

CATEGORIES: dict[str, set[str]] = {
    "adlaw": _ADLAW,
    "marketing": _MARKETING,
    "finance": _FINANCE,
    "medical": _MEDICAL,
}

# Level → which categories are enforced (platforms can map to a level).
LEVELS: dict[str, list[str]] = {
    "off": [],
    "basic": ["adlaw"],
    "standard": ["adlaw", "marketing", "finance"],
    "strict": ["adlaw", "marketing", "finance", "medical"],
}

# Optional per-platform default level (extensible).
PLATFORM_LEVELS = {
    "xiaohongshu": "strict",
    "douyin": "strict",
    "kuaishou": "strict",
    "bilibili": "standard",
    "wechat": "standard",
    "zhihu": "basic",
}


# A few restricted words are also the opening of perfectly ordinary phrases.
# The filter deletes what it matches, so masking one of these mangles the
# sentence ("第一次握住水杯" → "次握住水杯") without removing any claim. Matches
# followed by one of these are left alone; they are keyed by how the word ends
# so "全球第一" and "全国第一" inherit 第一's list.
_ORDINARY_AFTER: dict[str, tuple[str, ...]] = {
    "第一": ("次", "步", "时间", "手", "线", "章", "节", "部", "季", "集",
             "天", "轮", "批", "版", "期", "课", "讲", "人称", "现场",
             "作者", "语言", "阶段", "选择",
             # enumeration / list markers: 第一点 第一个 第一项 第一条 …
             "点", "个", "项", "条",
             # ordinary phrases: 第一句 第一反应 第一印象 第一段 第一页 第一场 第一站 第一题
             "句", "反应", "印象", "段", "页", "场", "站", "题"),
    "最高": ("法院", "人民法院", "气温", "温度", "时速", "海拔", "法规"),
    "最大": ("值", "化", "限度", "公约数", "公因数", "程度"),
}

# 「第一」is also an enumeration marker ("第一，第二，第三…" / "第一、第二…" /
# "第一：先说结论。第二：…" / "第一是效率，第二是成本"). The negative
# lookahead below additionally exempts 第一 when an ordinal word (第二…第十)
# follows shortly (≤40 chars, same line), so numbered lists survive — while
# ranking claims ("行业第一") with no following ordinal are still masked.
# Inherited by 全球第一/全国第一 via _ordinary_after()'s endswith matching.
_ENUM_SEQUENCE_AFTER = r".{0,40}第[二三四五六七八九十]"


def _ordinary_after(word: str) -> tuple[str, ...]:
    for stem, suffixes in _ORDINARY_AFTER.items():
        if word.endswith(stem):
            return suffixes
    return ()


@lru_cache(maxsize=512)
def _word_pattern(word: str) -> re.Pattern[str]:
    suffixes = _ordinary_after(word)
    if not suffixes:
        return re.compile(re.escape(word))
    tail = "|".join(re.escape(s) for s in suffixes)
    if word.endswith("第一"):
        # numbered lists ("第一，第二，第三…") keep 第一; ranking claims
        # ("行业第一") are still masked. Inherited by 全球第一/全国第一.
        tail += f"|{_ENUM_SEQUENCE_AFTER}"
    return re.compile(rf"{re.escape(word)}(?!{tail})")


def builtin_words(level: str = "standard") -> set[str]:
    words: set[str] = set()
    for cat in LEVELS.get((level or "standard").lower(), LEVELS["standard"]):
        words |= CATEGORIES.get(cat, set())
    return words


def _custom_from(raw: str | None) -> set[str]:
    if not raw:
        return set()
    parts = re.split(r"[,，\n;；]", raw)
    return {p.strip() for p in parts if p.strip()}


def load_words(config) -> set[str]:
    """Built-in words for the configured level + the user's custom additions."""
    level = (getattr(config, "sensitive_level", None) or "standard").lower()
    if level == "off":
        # custom words still apply even when built-ins are off
        return _custom_from(getattr(config, "sensitive_words", None))
    return builtin_words(level) | _custom_from(
        getattr(config, "sensitive_words", None))


def sanitize(text: str, words: set[str],
             replace: str = "") -> tuple[str, list[dict]]:
    """Remove/replace sensitive words in `text`. Returns (clean_text, hits)
    where hits = [{word, count, pos}] (pos = index of first occurrence)."""
    if not text or not words:
        return text or "", []
    out = text
    hits: list[dict] = []
    # longest-first so overlapping terms (e.g. 最好 vs 最) mask the longer one
    for w in sorted(words, key=len, reverse=True):
        if not w:
            continue
        pattern = _word_pattern(w)
        found = list(pattern.finditer(out))
        if found:
            hits.append({"word": w, "count": len(found), "pos": found[0].start()})
            out = pattern.sub(lambda _: replace, out)
    return out, hits


def sanitize_draft(draft, words: set[str]) -> list[dict]:
    """Sanitize a Draft's body + title candidates in place. Returns hits and
    also stores them on draft.sensitive_hits (list of 'word×count' strings)."""
    if not words:
        return []
    all_hits: list[dict] = []
    body, hits = sanitize(draft.body_md or "", words)
    draft.body_md = body
    all_hits.extend(hits)
    if draft.title_candidates:
        new_titles = []
        renamed: dict[str, str] = {}
        for t in draft.title_candidates:
            ct, th = sanitize(t, words)
            new_titles.append(ct)
            renamed[t] = ct
            all_hits.extend(th)
        draft.title_candidates = new_titles
        # Scores point at candidates by text; masking a word in one would
        # otherwise orphan its score.
        for entry in getattr(draft, "title_scores", None) or []:
            if isinstance(entry, dict) and entry.get("title") in renamed:
                entry["title"] = renamed[entry["title"]]
    if getattr(draft, "digest", ""):
        draft.digest, digest_hits = sanitize(draft.digest, words)
        all_hits.extend(digest_hits)
    # merge counts per word
    merged: dict[str, int] = {}
    for h in all_hits:
        merged[h["word"]] = merged.get(h["word"], 0) + h["count"]
    draft.sensitive_hits = [f"{w}×{c}" for w, c in merged.items()]
    return all_hits
