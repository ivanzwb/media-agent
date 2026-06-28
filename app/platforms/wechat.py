from __future__ import annotations

from app.platforms import Platform
from app.platforms.registry import register


@register
class WeChat(Platform):
    id = "wechat"
    label = "公众号"

    def system_prompt(self) -> str:
        return (
            "你是微信公众号资深编辑。把给定的主稿改写成适合公众号的长图文："
            "结构清晰、有小标题、段落稍长、专业但通俗，适合深度阅读。"
        )

    @property
    def publish_url(self) -> str:
        return "https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit_v2&action=edit&isNew=1&type=10&lang=zh_CN"
