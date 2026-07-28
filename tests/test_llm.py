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


# ── /api/test-llm endpoint tests ────────────────────────────────────────────

import os
import tempfile


def _make_test_app(**config_overrides):
    """Create a test app with a fresh in-memory-ish temp DB."""
    from pathlib import Path
    from fastapi.testclient import TestClient
    from app.web.server import create_app
    from app.config import Config
    tmp = tempfile.mkdtemp()
    cfg = Config(data_dir=Path(tmp), **config_overrides)
    app = create_app(config=cfg)
    return TestClient(app), tmp


def test_test_llm_mock_provider():
    """Mock provider returns ok without calling any API."""
    client, tmp = _make_test_app(llm_provider="mock")
    r = client.post("/api/test-llm")
    d = r.json()
    assert d["ok"] is True
    assert d.get("model") == "mock"


def test_test_llm_no_api_key():
    """Missing API key returns error."""
    client, tmp = _make_test_app(llm_provider="openai", llm_api_key="", llm_model="gpt-4o")
    r = client.post("/api/test-llm")
    d = r.json()
    assert d["ok"] is False
    assert "API Key" in d["error"]
