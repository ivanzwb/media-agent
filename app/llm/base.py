from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


class LLMProvider(Protocol):
    def chat(self, messages: list[Message], **opts) -> str:
        ...


def get_provider(name: str, api_key: str | None = None,
                 model: str | None = None) -> LLMProvider:
    if name == "mock":
        from app.llm.providers.mock import MockProvider
        return MockProvider()
    if name == "openai":
        from app.llm.providers.openai import OpenAIProvider
        return OpenAIProvider(api_key=api_key, model=model)
    raise ValueError(f"Unknown LLM provider: {name}")
