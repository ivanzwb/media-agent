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
