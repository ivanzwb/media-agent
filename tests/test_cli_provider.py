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
    """_run strips null bytes from prompt before passing to subprocess.

    _run spawns via subprocess.Popen (a per-call local handle, for thread
    safety), so we patch Popen and capture the argv it receives.
    """
    p = CLIProvider("opencode")

    captured = {}

    class FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args
            self.returncode = 0

        def communicate(self, timeout=None):
            return ("ok", "")

        def poll(self):
            return self.returncode

        def kill(self):
            ...

        def wait(self, timeout=None):
            return 0

    with patch.object(subprocess, "Popen", FakePopen):
        p._run("hello\x00world\x00", timeout=30)

    # The prompt is embedded in the argv list via {prompt} substitution
    args = captured["args"]              # [exe_path, ...substituted args...]
    arg_str = " ".join(args)
    assert "\x00" not in arg_str
    assert "helloworld" in arg_str


def test_run_can_allow_empty_stdout_for_file_writing_agents():
    """Agent workflows may receive the result through output-draft.md."""
    p = CLIProvider("opencode")

    class FakePopen:
        def __init__(self, args, **kwargs):
            self.returncode = 0

        def communicate(self, timeout=None):
            return ("", "")

        def poll(self):
            return self.returncode

    with patch.object(subprocess, "Popen", FakePopen):
        assert p._run(
            "write the file", timeout=30, allow_empty_output=True) == ""

    with patch.object(subprocess, "Popen", FakePopen):
        with pytest.raises(RuntimeError, match="empty output"):
            p._run("normal completion", timeout=30)


def _scripted_popen(results, on_call=None):
    """Popen stub replaying one (returncode, stdout, stderr) entry per call.

    A ``BaseException`` in the returncode slot is raised from communicate()
    instead, which is how a timeout is simulated.
    """
    calls = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            self.returncode = 0
            calls.append(args)
            if on_call is not None:
                on_call(len(calls))

        def communicate(self, timeout=None):
            outcome = results[len(calls) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            self.returncode, stdout, stderr = outcome
            return (stdout, stderr)

        def poll(self):
            return self.returncode

        def kill(self):
            ...

        def wait(self, timeout=None):
            return 0

    return FakePopen, calls


def test_an_api_model_name_falls_back_to_the_tool_default():
    """opencode addresses models as provider/model.

    A bare name comes from the OpenAI-compatible settings, and opencode
    answers an unqualified `Unexpected server error` rather than saying the
    model is unknown.
    """
    p = CLIProvider("opencode", model="agnes-2.0-flash")
    fake, calls = _scripted_popen([(0, "done", "")])

    with patch.object(subprocess, "Popen", fake):
        assert p._run("edit the draft", timeout=30) == "done"

    argv = calls[0]
    assert "agnes-2.0-flash" not in argv
    assert argv[argv.index("--model") + 1] == "opencode/big-pickle"


def test_a_qualified_model_is_passed_through():
    p = CLIProvider("opencode", model="opencode/deepseek-v4-flash-free")
    fake, calls = _scripted_popen([(0, "done", "")])

    with patch.object(subprocess, "Popen", fake):
        p._run("edit the draft", timeout=30)

    argv = calls[0]
    assert argv[argv.index("--model") + 1] == "opencode/deepseek-v4-flash-free"


def test_a_failure_names_the_model_it_used():
    """`Unexpected server error` alone leaves nowhere to look."""
    p = CLIProvider("opencode")
    fake, _calls = _scripted_popen([(1, "", "boom")] * 3)

    with patch.object(subprocess, "Popen", fake):
        with pytest.raises(RuntimeError, match=r"model: opencode/big-pickle"):
            p._run("edit the draft", timeout=30)


def test_a_transient_server_error_is_retried():
    """The CLI can die before it ever reaches the model — try again."""
    p = CLIProvider("opencode")
    fake, calls = _scripted_popen([
        (1, "", "Unexpected server error. Check server logs for details."),
        (0, "done", ""),
    ])

    with patch.object(subprocess, "Popen", fake), \
            patch("app.llm.providers.cli.time.sleep"):
        assert p._run("edit the draft", timeout=30) == "done"

    assert len(calls) == 2


def test_a_transient_error_reported_as_a_json_event_is_retried():
    """opencode --format json writes its real error to stdout, not stderr."""
    p = CLIProvider("opencode")
    error_event = (
        '{"type":"error","error":{"data":{"message":'
        '"Unexpected server error. Check server logs for details."}}}'
    )
    fake, calls = _scripted_popen([(1, error_event, ""), (0, "done", "")])

    with patch.object(subprocess, "Popen", fake), \
            patch("app.llm.providers.cli.time.sleep"):
        assert p._run("edit the draft", timeout=30) == "done"

    assert len(calls) == 2


def test_retries_are_capped_and_the_last_error_surfaces():
    p = CLIProvider("opencode")
    fake, calls = _scripted_popen([(1, "", "503 service unavailable")] * 5)

    with patch.object(subprocess, "Popen", fake), \
            patch("app.llm.providers.cli.time.sleep"):
        with pytest.raises(RuntimeError, match="service unavailable"):
            p._run("edit the draft", timeout=30)

    assert len(calls) == 3


def test_a_rejected_prompt_is_not_retried():
    """Nothing about the request changes on a second attempt."""
    p = CLIProvider("opencode")
    fake, calls = _scripted_popen([(1, "", "unknown model: nope")] * 3)

    with patch.object(subprocess, "Popen", fake), \
            patch("app.llm.providers.cli.time.sleep"):
        with pytest.raises(RuntimeError, match="unknown model"):
            p._run("edit the draft", timeout=30)

    assert len(calls) == 1


def test_a_timeout_is_not_retried():
    """The caller's whole time budget is already spent."""
    p = CLIProvider("opencode")
    fake, calls = _scripted_popen(
        [subprocess.TimeoutExpired(cmd="opencode", timeout=30)] * 3)

    with patch.object(subprocess, "Popen", fake), \
            patch("app.llm.providers.cli.time.sleep"):
        with pytest.raises(RuntimeError, match="timed out"):
            p._run("edit the draft", timeout=30)

    assert len(calls) == 1


def test_a_cancelled_run_is_not_retried():
    p = CLIProvider("opencode")
    fake, calls = _scripted_popen(
        [(1, "", "Unexpected server error")] * 3,
        on_call=lambda n: p.cancel(),
    )

    with patch.object(subprocess, "Popen", fake), \
            patch("app.llm.providers.cli.time.sleep"):
        with pytest.raises(RuntimeError, match="Unexpected server error"):
            p._run("edit the draft", timeout=30)

    assert len(calls) == 1


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
