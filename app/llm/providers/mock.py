from __future__ import annotations

from app.llm.base import Message


class MockProvider:
    def __init__(self, responses: list[str] | None = None):
        self._responses = list(responses) if responses else None
        self._i = 0

    def chat(self, messages: list[Message], **opts) -> str:
        if self._responses is not None:
            out = self._responses[min(self._i, len(self._responses) - 1)]
            self._i += 1
            return out
        last = messages[-1].content if messages else ""
        return f"[mock] {last}"
