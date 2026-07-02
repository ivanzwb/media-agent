from __future__ import annotations

from app.platforms import Platform
from app.platforms.registry import register


@register
class Toutiao(Platform):
    id = "toutiao"
    label = "头条"

    def system_prompt(self) -> str:
        return (
            "你是今日头条资深编辑。把主稿改写成头条号风格："
            "标题抓人眼球、带数字或设问句、三段式结构、"
            "口语化但不失权威、适合碎片化阅读。"
        )

    @property
    def publish_url(self) -> str:
        return "https://mp.toutiao.com/profile_v4/graphic/publish"

    @property
    def video_publish_url(self) -> str:
        # 头条创作中心；进入后点左侧「创作 → 视频」发布视频。
        return "https://mp.toutiao.com/"

    def video_caption_style(self) -> str:
        return (
            "你是今日头条视频编辑。标题要抓人、可含数字或设问；简介口语化、"
            "简短有信息量；配 3-5 个话题标签。不要标题党，不要绝对化用语。"
        )
