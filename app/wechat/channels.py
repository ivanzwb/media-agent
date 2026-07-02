"""WeChat Channels (视频号) half-automatic publish helper.

WeChat provides NO server-side API to publish a video to 视频号 (verified: the
视频号助手 API only exposes live/lead/e-commerce data, not video posting). So
this module only *prepares* the publish: it generates a 视频号-style caption
(short title + spoken-style description + hashtags) from the draft. The web UI
then copies the caption to the clipboard and opens the 视频号助手 create page,
where the user drags in the video file and pastes — no account automation, no
ban risk.
"""
from __future__ import annotations

from app.llm.base import LLMProvider
from app.platforms.video_prepare import build_video_caption

# 视频号助手 "发表" page — opened in a new tab for the user to finish manually.
CHANNELS_CREATE_URL = "https://channels.weixin.qq.com/platform/post/create"

_SYSTEM = (
    "你是微信视频号文案编辑。视频号是竖屏短视频平台，简介要口语化、简短有信息量，"
    "并带 2-4 个 #话题标签。不要浮夸、不要绝对化用语。"
)


def build_channels_caption(meta: dict,
                           provider: LLMProvider | None = None) -> dict:
    """Build a 视频号 caption from a draft (delegates to the generic builder).

    Returns {title, description, hashtags, caption}. Uses the LLM when
    available; otherwise falls back to deterministic text from the draft.
    """
    return build_video_caption(meta, provider, _SYSTEM)
