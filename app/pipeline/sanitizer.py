from __future__ import annotations

import re

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
        count = out.count(w)
        if count:
            hits.append({"word": w, "count": count, "pos": out.find(w)})
            out = out.replace(w, replace)
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
        for t in draft.title_candidates:
            ct, th = sanitize(t, words)
            new_titles.append(ct)
            all_hits.extend(th)
        draft.title_candidates = new_titles
    if getattr(draft, "digest", ""):
        draft.digest, digest_hits = sanitize(draft.digest, words)
        all_hits.extend(digest_hits)
    # merge counts per word
    merged: dict[str, int] = {}
    for h in all_hits:
        merged[h["word"]] = merged.get(h["word"], 0) + h["count"]
    draft.sensitive_hits = [f"{w}×{c}" for w, c in merged.items()]
    return all_hits
