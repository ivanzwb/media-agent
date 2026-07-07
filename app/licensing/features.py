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
VOICE_CLONE = "voice_clone"                # 声音复刻 TTS (cosyvoice/fishaudio)
SCHEDULE = "schedule"                      # 定时调度

ALL_FEATURES = [REWRITE_UNLIMITED, VIDEO, PLATFORM_SYNC, VOICE_CLONE, SCHEDULE]

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
}


def label(feature: str) -> str:
    return LABELS.get(feature, feature)
