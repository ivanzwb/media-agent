from __future__ import annotations

from openai import OpenAI

from app.llm.base import Message


class OpenAIProvider:
    def __init__(self, api_key: str | None = None,
                 model: str | None = None):
        self.client = OpenAI(api_key=api_key)
        self.model = model or "gpt-4o-mini"

    def chat(self, messages: list[Message], **opts) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": m.role, "content": m.content} for m in messages],
            temperature=opts.get("temperature", 0.7),
        )
        return resp.choices[0].message.content or ""
