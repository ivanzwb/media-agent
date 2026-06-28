from __future__ import annotations

from app.platforms import Platform
from app.platforms.registry import register


@register
class XiaoHongShu(Platform):
    id = "xiaohongshu"
    label = "小红书"

    def system_prompt(self) -> str:
        return (
            "你是小红书爆款写手。把主稿改写成小红书风格："
            "标题强冲击力、多 emoji、短句、分点、口语化，"
            "结尾配 3-6 个话题标签（#xxx）。"
        )

    @property
    def publish_url(self) -> str:
        return "https://creator.xiaohongshu.com/publish/publish"

    def clipboard_text(self, body_md: str, title: str) -> str:
        # Xiaohongshu doesn't support markdown — strip to plain text
        import re
        text = re.sub(r"[*_~`#>\[\]|]", "", body_md)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return f"{title}\n\n{text}"
