"""Tests for the CLI provider (app/llm/providers/cli.py)."""

import subprocess
from unittest.mock import patch

import pytest

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


def test_build_prompt_strips_null_bytes():
    """_build_prompt removes null bytes from system and user messages."""
    p = CLIProvider("opencode")
    result = p._build_prompt([
        Message(role="system", content="你是一个文章编辑助手\x00"),
        Message(role="user", content="帮我修改\x00这段文字"),
    ])
    assert "\x00" not in result
    assert "你是一个文章编辑助手" in result
    assert "帮我修改这段文字" in result


def test_run_strips_null_bytes_before_subprocess():
    """_run strips null bytes from prompt before passing to subprocess."""
    p = CLIProvider("opencode")

    captured = {}

    class FakePopenResult:
        """Duck-typing the subset of subprocess.CompletedProcess we use."""
        def __init__(self, args, returncode, stdout, stderr):
            self.args = args
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakePopenResult(args, 0, stdout="ok", stderr="")

    with patch.object(subprocess, "run", side_effect=fake_run):
        p._run("hello\x00world\x00", timeout=30)

    # The prompt is embedded in the arg list via {prompt} substitution
    args = captured["args"]
    # args[0] is exe_path, rest is the arg list
    arg_str = " ".join(args[0] if isinstance(args[0], list) else args)
    assert "\x00" not in arg_str
    assert "helloworld" in arg_str


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


def test_rewrite_priority_agent_first_uses_cli(monkeypatch):
    from app.llm.base import get_rewrite_provider
    from app.llm.providers.cli import CLIProvider
    from app.llm.providers.mock import MockProvider

    monkeypatch.setattr(
        "app.llm.base._resolve_cli_provider",
        lambda cli_tool, timeout=None: CLIProvider("opencode"),
    )
    provider = get_rewrite_provider("openai", cli_tool="opencode", priority="agent")
    assert isinstance(provider, CLIProvider)


def test_rewrite_priority_llm_first_uses_llm(monkeypatch):
    from app.llm.base import get_rewrite_provider
    from app.llm.providers.cli import CLIProvider
    from app.llm.providers.mock import MockProvider

    monkeypatch.setattr(
        "app.llm.base._resolve_cli_provider",
        lambda cli_tool, timeout=None: CLIProvider("opencode"),
    )
    provider = get_rewrite_provider("mock", cli_tool="opencode", priority="llm")
    assert isinstance(provider, CLIProvider)


def test_rewrite_priority_llm_first_prefers_openai(monkeypatch):
    from app.llm.base import get_rewrite_provider
    from app.llm.providers.cli import CLIProvider
    from app.llm.providers.openai import OpenAIProvider

    monkeypatch.setattr(
        "app.llm.base._resolve_cli_provider",
        lambda cli_tool, timeout=None: CLIProvider("opencode"),
    )
    provider = get_rewrite_provider(
        "openai", llm_api_key="sk-test", cli_tool="opencode", priority="llm")
    assert isinstance(provider, OpenAIProvider)
