"""Platform adapter — thin compatibility layer over app.platforms.registry."""

from __future__ import annotations

from app.llm.base import LLMProvider
from app.platforms.registry import get, list_all

# Expose for backward compatibility.
PLATFORMS = {p.id: p.system_prompt() for p in list_all()}


def adapt(master_md: str, title: str, platform: str,
          provider: LLMProvider) -> dict:
    p = get(platform)
    if p is None:
        raise ValueError(f"Unknown platform: {platform}")
    return p.convert(provider, master_md, title)
