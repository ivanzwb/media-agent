"""Platform abstraction — each platform defines content conversion + publish info."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.llm.base import LLMProvider


class Platform(ABC):
    """Abstract base for a content platform (e.g. WeChat, Xiaohongshu)."""

    id: str          # wechat, xiaohongshu, toutiao, zhihu ...
    label: str       # 公众号, 小红书, 头条, 知乎 ...

    @abstractmethod
    def system_prompt(self) -> str:
        """LLM system prompt for converting a master draft to this platform."""
        ...

    def convert(self, provider: LLMProvider, master_md: str,
                title: str) -> dict:
        """Convert a master draft to this platform's format via LLM.

        Returns ``{"title_candidates": [...], "body_md": "..."}``.
        """
        from app.llm.base import Message
        from app.pipeline.rewriter import _extract_json

        instruction = (
            "请基于且仅基于下面的主稿内容改写，不得编造主稿没有的事实、数据或结论。\n"
            "输出严格 JSON：{\"title_candidates\": [2-3 个标题], "
            "\"body_md\": \"Markdown 正文\"}，不要输出 JSON 以外内容。\n\n"
            f"原标题：{title}\n\n主稿：\n{master_md[:6000]}"
        )
        raw = provider.chat([
            Message(role="system", content=self.system_prompt()),
            Message(role="user", content=instruction),
        ])
        parsed = _extract_json(raw)
        if parsed and parsed.get("title_candidates") and parsed.get("body_md"):
            return {"title_candidates": parsed["title_candidates"],
                    "body_md": parsed["body_md"]}
        return {"title_candidates": [title], "body_md": raw}

    @property
    def publish_url(self) -> str | None:
        """URL of the platform's editor / creation page (None if N/A)."""
        return None

    @property
    def video_publish_url(self) -> str | None:
        """Creator video-publish page for half-automatic video posting
        (None if the platform has no video half-auto flow)."""
        return None

    def video_caption_style(self) -> str:
        """LLM system prompt for a short video caption on this platform.
        Defaults to the article style; platforms may override."""
        return self.system_prompt()

    def clipboard_text(self, body_md: str, title: str) -> str:
        """Plain-text representation for clipboard copy."""
        return f"{title}\n\n{body_md}"
