import pytest

from app.llm.base import get_provider, Message
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
