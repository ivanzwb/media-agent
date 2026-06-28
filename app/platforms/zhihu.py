from __future__ import annotations

from app.platforms import Platform
from app.platforms.registry import register


@register
class Zhihu(Platform):
    id = "zhihu"
    label = "知乎"

    def system_prompt(self) -> str:
        return (
            "你是知乎专栏作者。把主稿改写成知乎深度回答风格："
            "有观点、有逻辑递进、引用主稿中的事实佐证，"
            "理性克制、信息密度高。"
        )

    @property
    def publish_url(self) -> str:
        return "https://zhuanlan.zhihu.com/write"
