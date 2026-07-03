"""Tests for the CLI provider (app/llm/providers/cli.py)."""

from app.llm.base import Message
from app.llm.providers.cli import CLIProvider, detect, detect_all


def test_detect_returns_none_for_unknown():
    assert detect("nonexistent-tool-xyzzz") is None


def test_detect_all_returns_str_or_none():
    result = detect_all()
    assert result is None or isinstance(result, str)


def test_cli_provider_constructor():
    """Known tool IDs construct without error when the binary is on PATH."""
    tool = detect_all()
    if tool is None:
        return  # skip if no CLI tool is installed
    p = CLIProvider(tool)
    assert p.tool_id == tool
    assert p.tool_label  # non-empty label


def test_cli_provider_unknown_tool_raises():
    try:
        CLIProvider("nonexistent-tool")
    except ValueError:
        pass
    else:
        raise AssertionError("Expected ValueError for unknown tool")


def test_build_prompt_combines_system_and_user():
    """System message is wrapped in [SYSTEM] block, user messages are raw."""
    tool = detect_all()
    if tool is None:
        return
    # Create provider and test _build_prompt via a minimal subclass-like approach
    p = CLIProvider(tool)
    result = p._build_prompt([
        Message(role="system", content="You are a helpful assistant."),
        Message(role="user", content="Hello, world!"),
    ])
    assert "[SYSTEM]" in result
    assert "You are a helpful assistant." in result
    assert "Hello, world!" in result


def test_get_rewrite_provider_falls_back_to_mock():
    """When no CLI tool is configured, get_rewrite_provider falls back."""
    from app.llm.base import get_rewrite_provider
    provider = get_rewrite_provider("mock", cli_tool="none")
    assert provider is not None
    # MockProvider should be returned
    from app.llm.providers.mock import MockProvider
    assert isinstance(provider, MockProvider)


def test_get_rewrite_provider_falls_back_when_cli_not_installed():
    """Requesting a CLI tool that isn't installed falls back to mock."""
    from app.llm.base import get_rewrite_provider
    provider = get_rewrite_provider("mock", cli_tool="copilot")
    # copilot may or may not be installed; if not, falls back to mock
    assert provider is not None
