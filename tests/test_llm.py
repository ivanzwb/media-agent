from pathlib import Path

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


# ── /api/test-image endpoint tests ──────────────────────────────────────────


def test_test_image_mock_provider():
    """Mock provider reports availability without calling any API."""
    client, tmp = _make_test_app(image_provider="mock")
    d = client.post("/api/test-image").json()
    assert d["ok"] is True
    assert d["model"] == "mock"


def test_test_image_no_api_key():
    """A real provider without any usable key reports the missing key."""
    client, tmp = _make_test_app(
        image_provider="openai", image_api_key="", llm_api_key="")
    d = client.post("/api/test-image").json()
    assert d["ok"] is False
    assert "API Key" in d["error"]


def test_test_image_generates_probe_and_reports_size(monkeypatch):
    """A successful probe generates one image and leaves nothing behind."""
    generated: dict = {}

    class FakeProvider:
        def generate(self, prompt, out_path):
            generated["prompt"] = prompt
            generated["path"] = Path(out_path)
            Path(out_path).write_bytes(b"x" * 3072)
            return out_path

    monkeypatch.setattr(
        "app.web.server.get_image_provider",
        lambda *args, **kwargs: FakeProvider())
    client, tmp = _make_test_app(
        image_provider="openai", image_api_key="key", image_model="gpt-image-1")
    d = client.post("/api/test-image").json()
    assert d["ok"] is True
    assert d["model"] == "gpt-image-1"
    assert d["size_kb"] == 3
    assert generated["prompt"]
    assert not generated["path"].exists()


def test_test_image_reports_provider_failure(monkeypatch):
    """Provider errors surface as a message instead of a 500."""
    def explode(*args, **kwargs):
        raise RuntimeError("model not found")

    monkeypatch.setattr("app.web.server.get_image_provider", explode)
    client, tmp = _make_test_app(
        image_provider="openai", image_api_key="key")
    d = client.post("/api/test-image").json()
    assert d["ok"] is False
    assert "model not found" in d["error"]


def test_test_image_falls_back_to_llm_credentials(monkeypatch):
    """Image settings inherit the LLM key/base, matching cover generation."""
    captured: dict = {}

    class FakeProvider:
        def generate(self, prompt, out_path):
            Path(out_path).write_bytes(b"png")
            return out_path

    def fake_get(name, api_key=None, *, model=None, base_url=None):
        captured.update(
            name=name, api_key=api_key, model=model, base_url=base_url)
        return FakeProvider()

    monkeypatch.setattr("app.web.server.get_image_provider", fake_get)
    client, tmp = _make_test_app(
        image_provider="openai", image_api_key="", image_api_base="",
        llm_api_key="shared-key", llm_api_base="https://proxy.test/v1")
    assert client.post("/api/test-image").json()["ok"] is True
    assert captured["api_key"] == "shared-key"
    assert captured["base_url"] == "https://proxy.test/v1"
