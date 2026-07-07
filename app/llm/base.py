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
                 model: str | None = None,
                 base_url: str | None = None,
                 timeout: int | None = None) -> LLMProvider:
    if name == "mock":
        from app.llm.providers.mock import MockProvider
        return MockProvider()
    if name == "openai":
        from app.llm.providers.openai import OpenAIProvider
        return OpenAIProvider(api_key=api_key, model=model, base_url=base_url)
    # CLI-agent providers (opencode / codex / copilot)
    from app.llm.providers.cli import detect_all as _cli_detect_all
    if name in ("opencode", "codex", "copilot"):
        from app.llm.providers.cli import CLIProvider, detect
        path = detect(name)
        if path is None:
            raise RuntimeError(
                f"CLI tool '{name}' is not installed ({path or 'not found in PATH'})")
        kwargs = {"model": model or ""}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return CLIProvider(name, **kwargs)
    if name == "auto":
        from app.llm.providers.cli import CLIProvider, detect_all
        tool = detect_all()
        if tool:
            kwargs = {}
            if timeout is not None:
                kwargs["timeout"] = timeout
            return CLIProvider(tool, **kwargs)
        # Fall through to LLM provider below
    if name == "auto":
        from app.llm.providers.mock import MockProvider
        return MockProvider()
    raise ValueError(f"Unknown LLM provider: {name}")


def get_rewrite_provider(llm_provider: str, llm_api_key: str | None = None,
                         llm_model: str | None = None,
                         llm_api_base: str | None = None,
                         cli_tool: str | None = None,
                         timeout: int | None = None) -> LLMProvider:
    """Return the best available provider for article rewriting.

    Priority:
    1. CLI agent (opencode / codex / copilot) if *cli_tool* is set and the
       tool is installed.
    2. The configured LLM provider.

    Falls back to MockProvider when nothing else is available.
    """
    if cli_tool and cli_tool not in ("none", ""):
        from app.llm.providers.cli import detect_all, detect
        tool_id = cli_tool if cli_tool != "auto" else detect_all()
        if tool_id:
            path = detect(tool_id)
            if path is not None:
                from app.llm.providers.cli import CLIProvider
                # Don't pass llm_model here — CLI tools have their own
                # model defaults (e.g. opencode/big-pickle) that are
                # independent of the user's LLM provider model config.
                kwargs = {}
                if timeout is not None:
                    kwargs["timeout"] = timeout
                return CLIProvider(tool_id, **kwargs)
    # Fall back to regular LLM provider (or mock)
    try:
        return get_provider(llm_provider, api_key=llm_api_key,
                            model=llm_model, base_url=llm_api_base,
                            timeout=timeout)
    except (ValueError, RuntimeError):
        from app.llm.providers.mock import MockProvider
        return MockProvider()
