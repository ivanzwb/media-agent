from unittest.mock import MagicMock, patch

import pytest
from openai import APIConnectionError, APITimeoutError, RateLimitError

from app.llm.base import Message
from app.llm.providers.openai import OpenAIProvider


class FakeCompletions:
    def create(self, **kwargs):
        class Msg:
            content = "openai reply"

        class Choice:
            message = Msg()

        class Resp:
            choices = [Choice()]
        self.kwargs = kwargs
        return Resp()


class FakeChat:
    def __init__(self):
        self.completions = FakeCompletions()


class FakeClient:
    def __init__(self, **kwargs):
        self.chat = FakeChat()
        self._kwargs = kwargs


def test_openai_chat_uses_client(monkeypatch):
    monkeypatch.setattr("app.llm.providers.openai.OpenAI", FakeClient)
    p = OpenAIProvider(api_key="sk-test", model="gpt-4o-mini")
    out = p.chat([Message(role="user", content="hi")])
    assert out == "openai reply"


# ── constructor forwarding tests ────────────────────────────────────────────

# Store for capturing kwargs from the fake client constructor.
_last_client_kwargs: dict = {}


class _CapturingClient:
    def __init__(self, **kwargs):
        _last_client_kwargs.clear()
        _last_client_kwargs.update(kwargs)
        self.chat = FakeChat()


def _setup_capture(monkeypatch):
    _last_client_kwargs.clear()
    monkeypatch.setattr("app.llm.providers.openai.OpenAI", _CapturingClient)


def test_openai_chat_passes_timeout(monkeypatch):
    _setup_capture(monkeypatch)
    OpenAIProvider(api_key="sk-test", timeout=180.0)
    assert _last_client_kwargs["timeout"] == 180.0


def test_openai_chat_passes_max_retries(monkeypatch):
    _setup_capture(monkeypatch)
    OpenAIProvider(api_key="sk-test", max_retries=5)
    assert _last_client_kwargs["max_retries"] == 5


def test_openai_chat_passes_base_url(monkeypatch):
    _setup_capture(monkeypatch)
    OpenAIProvider(api_key="sk-test", base_url="https://api.example.com/v1")
    assert _last_client_kwargs["base_url"] == "https://api.example.com/v1"


def test_openai_chat_default_timeout(monkeypatch):
    _setup_capture(monkeypatch)
    OpenAIProvider(api_key="sk-test")
    assert _last_client_kwargs["timeout"] == 120.0


# ── retry tests ─────────────────────────────────────────────────────────────


def _make_failing_completions(exc_factory, fail_count=2):
    """Return a FakeCompletions that raises *exc_factory()* *fail_count* times,
    then succeeds."""
    class _Completions:
        def __init__(self):
            self.call_count = 0

        def create(self, **kwargs):
            self.call_count += 1
            if self.call_count <= fail_count:
                raise exc_factory()
            class Msg:
                content = "recovered"
            class Choice:
                message = Msg()
            class Resp:
                choices = [Choice()]
            return Resp()
    return _Completions()


def _make_always_failing_completions(exc_factory):
    """Completions that always raises."""
    class _Completions:
        def __init__(self):
            self.call_count = 0

        def create(self, **kwargs):
            self.call_count += 1
            raise exc_factory()
    return _Completions()


class _FakeChatForRetry:
    def __init__(self, completions):
        self.completions = completions


class _FakeClientForRetry:
    def __init__(self, completions, **kwargs):
        self.chat = _FakeChatForRetry(completions)


def _patch_client(monkeypatch, completions):
    """Monkeypatch OpenAI constructor to use our fake client."""
    def factory(**kwargs):
        return _FakeClientForRetry(completions, **kwargs)
    monkeypatch.setattr("app.llm.providers.openai.OpenAI", factory)


def _make_connection_error():
    """Create an APIConnectionError with required args."""
    request = MagicMock()
    return APIConnectionError(request=request)


def test_retry_recovers_from_connection_error(monkeypatch):
    """Provider retries on APIConnectionError and succeeds."""
    comps = _make_failing_completions(_make_connection_error, fail_count=2)
    _patch_client(monkeypatch, comps)
    p = OpenAIProvider(api_key="sk-test", max_retries=3)
    with patch("app.llm.providers.openai.time.sleep"):
        out = p.chat([Message(role="user", content="hi")])
    assert out == "recovered"
    assert comps.call_count == 3  # 2 failures + 1 success


def _make_timeout_error():
    """Create an APITimeoutError with required request arg."""
    request = MagicMock()
    return APITimeoutError(request=request)


def test_retry_recovers_from_timeout_error(monkeypatch):
    """Provider retries on APITimeoutError and succeeds."""
    comps = _make_failing_completions(_make_timeout_error, fail_count=1)
    _patch_client(monkeypatch, comps)
    p = OpenAIProvider(api_key="sk-test", max_retries=3)
    with patch("app.llm.providers.openai.time.sleep"):
        out = p.chat([Message(role="user", content="hi")])
    assert out == "recovered"
    assert comps.call_count == 2


def test_retry_recovers_from_rate_limit(monkeypatch):
    """Provider retries on RateLimitError and succeeds."""
    comps = _make_failing_completions(
        lambda: RateLimitError(
            message="rate limited",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        ),
        fail_count=1,
    )
    _patch_client(monkeypatch, comps)
    p = OpenAIProvider(api_key="sk-test", max_retries=3)
    with patch("app.llm.providers.openai.time.sleep"):
        out = p.chat([Message(role="user", content="hi")])
    assert out == "recovered"
    assert comps.call_count == 2


def test_retry_exhausted_raises(monkeypatch):
    """Provider raises the last exception when all retries exhausted."""
    comps = _make_always_failing_completions(_make_connection_error)
    _patch_client(monkeypatch, comps)
    p = OpenAIProvider(api_key="sk-test", max_retries=2)
    with patch("app.llm.providers.openai.time.sleep"):
        with pytest.raises(APIConnectionError):
            p.chat([Message(role="user", content="hi")])
    assert comps.call_count == 3  # 1 initial + 2 retries


def test_retry_exponential_backoff_delays(monkeypatch):
    """Provider uses exponential backoff: 2s, 4s, 8s."""
    comps = _make_always_failing_completions(_make_connection_error)
    _patch_client(monkeypatch, comps)
    p = OpenAIProvider(api_key="sk-test", max_retries=3)
    sleeps = []
    with patch("app.llm.providers.openai.time.sleep", side_effect=lambda s: sleeps.append(s)):
        with pytest.raises(APIConnectionError):
            p.chat([Message(role="user", content="hi")])
    assert sleeps == [2.0, 4.0, 8.0]


def test_no_retry_on_success(monkeypatch):
    """No retries when first call succeeds."""
    comps = _make_failing_completions(_make_connection_error, fail_count=0)
    _patch_client(monkeypatch, comps)
    p = OpenAIProvider(api_key="sk-test", max_retries=3)
    with patch("app.llm.providers.openai.time.sleep") as mock_sleep:
        out = p.chat([Message(role="user", content="hi")])
    assert out == "recovered"
    mock_sleep.assert_not_called()


def test_non_retryable_error_raises_immediately(monkeypatch):
    """Non-retryable errors (e.g. ValueError) are raised without retry."""
    call_count = 0

    class _Completions:
        def create(self, **kwargs):
            nonlocal call_count
            call_count += 1
            raise ValueError("bad model")
    _patch_client(monkeypatch, _Completions())
    p = OpenAIProvider(api_key="sk-test", max_retries=3)
    with pytest.raises(ValueError, match="bad model"):
        p.chat([Message(role="user", content="hi")])
    assert call_count == 1  # no retry
