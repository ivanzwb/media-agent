from __future__ import annotations

import logging
import time

from openai import OpenAI, APIConnectionError, APITimeoutError, RateLimitError

from app.llm.base import Message

logger = logging.getLogger(__name__)

# Default retry configuration for flaky third-party APIs.
_MAX_RETRIES = 3
_BASE_DELAY = 2.0   # seconds — exponential backoff base


class OpenAIProvider:
    def __init__(self, api_key: str | None = None,
                 model: str | None = None,
                 base_url: str | None = None,
                 timeout: float = 120.0,
                 max_retries: int = _MAX_RETRIES):
        kwargs = {"api_key": api_key, "timeout": timeout,
                  "max_retries": max_retries}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)
        self.model = model or "gpt-4o-mini"
        self.max_retries = max_retries

    def chat(self, messages: list[Message], **opts) -> str:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": m.role, "content": m.content}
                              for m in messages],
                    temperature=opts.get("temperature", 0.7),
                )
                return resp.choices[0].message.content or ""
            except (APIConnectionError, APITimeoutError, RateLimitError) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    delay = _BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        "OpenAI chat attempt %d/%d failed (%s), retrying in %.1fs…",
                        attempt + 1, self.max_retries + 1, type(exc).__name__, delay,
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        "OpenAI chat failed after %d attempts: %s",
                        self.max_retries + 1, exc,
                    )
        raise last_exc  # type: ignore[misc]
