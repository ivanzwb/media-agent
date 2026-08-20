from __future__ import annotations

import json
import re
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from app.llm.base import LLMProvider, Message
from app.pipeline.ai_flavor import check_text
from app.pipeline.diversion import check_draft
from app.pipeline.terminology import check_terms

logger = logging.getLogger(__name__)

# 低于这个分数的 AI 味不值得打断创作日志：谁写东西都会偶尔冒一句套话，
# 报得太勤，真正偏重的那几篇反而淹了。对应 ai_flavor 的「偏重」档。
FLAVOR_NOTICE_AT = 45

SCORING_PROMPT = (
    "你是资深自媒体内容评分专家。请对以下草稿进行多维度的综合评分（0-100 分），"
    "评分越高代表该文章越容易获得各大平台（公众号、头条、小红书、知乎等）的"
    "高频推荐和传播。\n\n"
    "评分维度及权重：\n"
    "1. **话题热度**（30%）— 该话题当前是否处于大众关注焦点、讨论度高"
    "2. **内容质量**（25%）— 文章结构清晰度、语句流畅度、逻辑严谨性、深度与干货密度\n"
    "3. **时效新鲜度**（20%）— 文章涉及的事件/信息是否近期发生，越新鲜评分越高\n"
    "4. **传播潜力**（15%）— 标题吸引力、钩子力度、是否容易引发转发和讨论\n"
    "5. **平台适配度**（10%）— 风格是否适合中文自媒体平台的推荐算法偏好\n\n"
    "评分标准参考：\n"
    "- 90-100：爆款级，几乎每个平台都愿意强力推荐\n"
    "- 75-89：优秀，有较大概率获得推荐\n"
    "- 50-74：良好，中规中矩，有一定推荐潜力\n"
    "- 25-49：一般，推荐可能性较低\n"
    "- 0-24：较差，很难获得推荐\n\n"
    "只返回 JSON 格式，不要输出任何额外文字：\n"
    '{{"score": 整数 0-100, "reasoning": "简要说明给分理由（一句话）"}}\n\n'
    "草稿标题：{title}\n"
    "文章主题：{topic}\n"
    "发布时间：{published_at}\n"
    "来源名称：{source_name}\n"
    "正文开头（前 500 字）：\n{body_preview}"
)


def _parse_score_json(raw: str) -> dict | None:
    """Extract JSON with score field from an LLM response."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def compute_draft_score(
    title: str = "",
    topic: str = "",
    source_name: str = "",
    body_preview: str = "",
    published_at: datetime | str | None = None,
    provider: LLMProvider | None = None,
) -> float:
    """Compute a comprehensive score (0-100) for a draft article.

    Uses LLM for multi-dimensional scoring when a non-mock provider is
    available; falls back to a heuristic baseline when not.

    Returns
    -------
    float: Score from 0 to 100 (rounded to 1 decimal place).
    """
    is_mock = provider is None or type(provider).__name__ == "MockProvider"

    if not is_mock:
        return _llm_score(title, topic, source_name, body_preview, published_at, provider)
    return _heuristic_score(title, topic, source_name, body_preview, published_at)


@dataclass(frozen=True)
class DraftGrade:
    """Everything that can be said about a finished draft without a human.

    ``ai_flavor`` is the :func:`app.pipeline.ai_flavor.check_text` report.
    ``terms`` is the :func:`app.pipeline.terminology.check_terms` list: the
    English left sitting in Chinese prose. Two rounds of prompt work went into
    that rule and both regressed unnoticed, because nothing looked at what came
    back out.
    """
    score: float
    diversion: list[dict]
    ai_flavor: dict
    terms: list[dict]


def grade_draft(draft, *, provider: LLMProvider | None = None,
                published_at: datetime | str | None = None,
                promotion_footer: str = "") -> DraftGrade:
    """Grade a finished draft: recommendation score, diversion, AI flavour.

    All three checks existed already, and none of them reached an article
    that was not rewritten from a single source. Scoring ran only on the
    rewrite path, while the diversion check and the AI-flavour check ran only
    when someone opened the draft and, for the latter, clicked a button. A
    search- or series-created article therefore arrived in the list with
    nothing said about it, and a bad one was indistinguishable from a good
    one until it was read.
    """
    score = compute_draft_score(
        title=draft.title_candidates[0] if draft.title_candidates else "",
        topic=draft.topic or "",
        source_name=draft.source_name or "",
        body_preview=draft.body_md,
        published_at=published_at,
        provider=provider,
    )
    findings = check_draft(
        {
            "title_candidates": draft.title_candidates,
            "title_cn": draft.title_cn or "",
            "digest": draft.digest or "",
            "body_md": draft.body_md,
        },
        promotion_footer,
    )
    body = draft.body_md or ""
    return DraftGrade(score, findings, check_text(body), check_terms(body))


def _llm_score(
    title: str,
    topic: str,
    source_name: str,
    body_preview: str,
    published_at: datetime | str | None,
    provider: LLMProvider,
) -> float:
    """Score via LLM call."""
    # Normalize published_at to string
    pub_str = ""
    if published_at:
        if isinstance(published_at, datetime):
            pub_str = published_at.strftime("%Y-%m-%d")
        else:
            pub_str = str(published_at)[:10]

    preview = (body_preview or "")[:500].strip()
    prompt_text = SCORING_PROMPT.format(
        title=(title or "无标题")[:100],
        topic=(topic or "未分类"),
        published_at=pub_str or "未知",
        source_name=(source_name or "未知来源"),
        body_preview=preview if preview else "(正文为空)",
    )
    try:
        raw = provider.chat([Message(role="user", content=prompt_text)])
        parsed = _parse_score_json(raw)
        if parsed and "score" in parsed:
            score = max(0.0, min(100.0, float(parsed["score"])))
            return round(score, 1)
    except Exception as exc:
        logger.warning("LLM scoring failed, falling back to heuristic: %s", exc)

    return _heuristic_score(title, topic, source_name, body_preview, published_at)


def _heuristic_score(
    title: str = "",
    topic: str = "",
    source_name: str = "",
    body_preview: str = "",
    published_at: datetime | str | None = None,
) -> float:
    """Heuristic scoring when no LLM provider is available.

    Combines:
    - Topic baseline (known hot topics get boost)
    - Freshness (newer = higher)
    - Content length / quality proxy (longer body = more substance)
    - Title quality (longer title = more specific)
    """
    score = 50.0  # baseline

    # ── Topic boost (covers hot topics as of 2026) ──
    hot_topics = [
        "AI", "人工智能", "大模型", "LLM", "GPT", "OpenAI", "Claude", "DeepSeek",
        "机器人", "具身智能", "自动驾驶", "AI Agent", "AIGC", "Sora",
        "半导体", "芯片", "新能源", "量子计算", "脑机接口", "人形机器人",
        "多模态", "视频生成", "AI编程", "商业航天",
    ]
    title_lower = title.lower()
    topic_lower = topic.lower()
    for ht in hot_topics:
        if ht.lower() in title_lower or ht.lower() in topic_lower:
            score += 10
            break

    # ── Freshness ──
    if published_at:
        if isinstance(published_at, str):
            try:
                published_at = datetime.fromisoformat(published_at)
            except (ValueError, TypeError):
                published_at = None
        if isinstance(published_at, datetime):
            age = datetime.now(timezone.utc) - published_at
            days = age.days
            if days <= 1:
                score += 20
            elif days <= 3:
                score += 15
            elif days <= 7:
                score += 10
            elif days <= 14:
                score += 5

    # ── Content quality proxy ──
    body_len = len(body_preview or "")
    if body_len > 2000:
        score += 10
    elif body_len > 1000:
        score += 5
    elif body_len < 100:
        score -= 10  # very short body

    # ── Title quality ──
    if len(title) >= 15:
        score += 5
    elif len(title) < 5:
        score -= 5

    return round(max(0.0, min(100.0, score)), 1)
