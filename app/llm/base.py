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


def _resolve_cli_provider(cli_tool: str | None,
                          timeout: int | None = None) -> LLMProvider | None:
    if not cli_tool or cli_tool in ("none", ""):
        return None
    from app.llm.providers.cli import CLIProvider, detect, detect_all
    tool_id = cli_tool if cli_tool != "auto" else detect_all()
    if not tool_id or detect(tool_id) is None:
        return None
    kwargs: dict = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return CLIProvider(tool_id, **kwargs)


def _resolve_llm_provider(llm_provider: str, llm_api_key: str | None = None,
                          llm_model: str | None = None,
                          llm_api_base: str | None = None,
                          timeout: int | None = None) -> LLMProvider | None:
    try:
        return get_provider(llm_provider, api_key=llm_api_key,
                            model=llm_model, base_url=llm_api_base,
                            timeout=timeout)
    except (ValueError, RuntimeError) as exc:
        import logging
        logging.warning("LLM provider '%s' unavailable (%s)",
                        llm_provider, exc)
        return None


def get_rewrite_provider(llm_provider: str, llm_api_key: str | None = None,
                         llm_model: str | None = None,
                         llm_api_base: str | None = None,
                         cli_tool: str | None = None,
                         timeout: int | None = None,
                         priority: str = "agent") -> LLMProvider:
    """Return the best available provider for article rewriting.

    *priority* controls which backend is tried first when both are configured:
    - ``agent``: CLI agent first, then LLM provider (default).
    - ``llm``: configured LLM provider first, then CLI agent.

    Mock LLM is only preferred under ``llm`` priority when no CLI agent is
    available. Falls back to MockProvider when nothing else works.
    """
    from app.llm.providers.mock import MockProvider

    priority = (priority or "agent").strip().lower()
    if priority not in ("agent", "llm"):
        priority = "agent"

    cli = _resolve_cli_provider(cli_tool, timeout=timeout)
    has_real_llm = llm_provider not in ("mock", "", None)

    if priority == "llm":
        if has_real_llm:
            llm = _resolve_llm_provider(
                llm_provider, llm_api_key, llm_model, llm_api_base, timeout)
            if llm is not None:
                return llm
        if cli is not None:
            return cli
        return _resolve_llm_provider(
            llm_provider, llm_api_key, llm_model, llm_api_base, timeout
        ) or MockProvider()

    if cli is not None:
        return cli
    return _resolve_llm_provider(
        llm_provider, llm_api_key, llm_model, llm_api_base, timeout
    ) or MockProvider()
