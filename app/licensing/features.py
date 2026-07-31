"""Feature catalogue + free/Pro tiers (issue #27 table).

Ungated core features (RSS/网页抓取, 去重归档, 主题分类, kitten TTS) are never
checked here — they always work. Only the Pro value-points below are gated.
Free tier additionally allows a small daily quota of LLM rewrites.
"""
from __future__ import annotations

# Gated feature ids.
REWRITE_UNLIMITED = "rewrite_unlimited"   # free: FREE_REWRITE_PER_DAY/day
VIDEO = "video"                            # 讲解视频生成
PLATFORM_SYNC = "platform_sync"            # 平台适配/一键同步/发布
VOICE_CLONE = "voice_clone"                # 声音复刻 TTS (cosyvoice)
SCHEDULE = "schedule"                      # 定时调度
AUTO_DISCOVER = "auto_discover"            # AI 智能推荐 + 一键/批量发现来源
SEARCH_CREATE = "search_create"             # 搜索驱动的多源综合创作
STYLE_LEARN = "style_learn"                 # 多篇样例学习转写风格
SERIES_CREATE = "series_create"             # 知识系列创作（逐章研究与写作）

ALL_FEATURES = [REWRITE_UNLIMITED, VIDEO, PLATFORM_SYNC, VOICE_CLONE,
                SCHEDULE, AUTO_DISCOVER, SEARCH_CREATE, STYLE_LEARN,
                SERIES_CREATE]

# Everything a Pro license grants.
PRO_FEATURES = set(ALL_FEATURES)

# Free tier: how many LLM rewrites per calendar day (local time).
FREE_REWRITE_PER_DAY = 1

# Human-readable labels for messages / UI.
LABELS = {
    REWRITE_UNLIMITED: "无限 LLM 改写",
    VIDEO: "讲解视频生成",
    PLATFORM_SYNC: "平台同步 / 一键发布",
    VOICE_CLONE: "声音复刻配音",
    SCHEDULE: "定时调度",
    AUTO_DISCOVER: "AI 智能推荐与一键发现来源",
    SEARCH_CREATE: "搜索驱动创作",
    STYLE_LEARN: "AI 学习与管理自定义转写风格",
    SERIES_CREATE: "知识系列创作",
}


def label(feature: str) -> str:
    return LABELS.get(feature, feature)
