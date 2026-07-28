import pytest

from app.llm.base import get_provider, get_rewrite_provider, Message
from app.llm.providers.mock import MockProvider


def test_get_provider_returns_mock():
    p = get_provider("mock")
    assert isinstance(p, MockProvider)


def test_mock_chat_echoes_deterministically():
    p = get_provider("mock")
    out1 = p.chat([Message(role="user", content="hello")])
    out2 = p.chat([Message(role="user", content="hello")])
    assert out1 == out2
    assert "hello" in out1


def test_mock_can_be_scripted():
    p = MockProvider(responses=["FIRST", "SECOND"])
    assert p.chat([Message(role="user", content="x")]) == "FIRST"
    assert p.chat([Message(role="user", content="y")]) == "SECOND"


def test_get_provider_unknown_raises():
    with pytest.raises(ValueError):
        get_provider("nope")


# ── get_rewrite_provider tests ──────────────────────────────────────────────


def test_rewrite_provider_returns_mock_when_nothing_configured():
    """Fallback to mock when no provider is configured."""
    p = get_rewrite_provider("mock", cli_tool="none")
    assert isinstance(p, MockProvider)


def test_rewrite_provider_mock_priority_llm():
    """With priority=llm and provider=mock, returns MockProvider."""
    p = get_rewrite_provider("mock", priority="llm")
    assert isinstance(p, MockProvider)


def test_rewrite_provider_openai_with_timeout(monkeypatch):
    """OpenAI provider receives llm_timeout when provided."""
    captured = {}
    orig_init = None

    class FakeCompletions:
        def create(self, **kwargs):
            class Msg:
                content = "ok"
            class Choice:
                message = Msg()
            class Resp:
                choices = [Choice()]
            return Resp()

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.chat = FakeChat()

    monkeypatch.setattr("app.llm.providers.openai.OpenAI", FakeClient)
    p = get_rewrite_provider(
        "openai", llm_api_key="sk-test", llm_model="gpt-4o",
        llm_timeout=240, priority="llm")
    p.chat([Message(role="user", content="hi")])
    assert captured["timeout"] == 240


def test_rewrite_provider_openai_uses_fallback_timeout(monkeypatch):
    """When llm_timeout is None, falls back to timeout parameter."""
    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            class Msg:
                content = "ok"
            class Choice:
                message = Msg()
            class Resp:
                choices = [Choice()]
            return Resp()

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.chat = FakeChat()

    monkeypatch.setattr("app.llm.providers.openai.OpenAI", FakeClient)
    p = get_rewrite_provider(
        "openai", llm_api_key="sk-test", llm_model="gpt-4o",
        timeout=300, llm_timeout=None, priority="llm")
    p.chat([Message(role="user", content="hi")])
    assert captured["timeout"] == 300
