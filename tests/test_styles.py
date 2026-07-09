"""Tests for the rewrite-styles feature (issue #30)."""
import json
from datetime import datetime, timezone

import pytest

from app.models import Article
from app.pipeline import styles as S
from app.pipeline.rewriter import (
    REWRITE_INSTRUCTION, REWRITE_SYSTEM, render_instruction, rewrite,
)


class FakeStore:
    """Minimal store implementing the settings key-value API used by styles."""

    def __init__(self):
        self.d: dict[str, str] = {}

    def get_setting(self, key, default=None):
        return self.d.get(key, default)

    def set_setting(self, key, value):
        self.d[key] = value

    def delete_setting(self, key):
        self.d.pop(key, None)


class RecordingProvider:
    """Captures the messages passed to chat() and returns canned responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0
        self.calls: list[list] = []

    def chat(self, messages, **opts):
        self.calls.append(messages)
        out = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return out


def _article():
    return Article(
        title="New AI Model", content_md="The model scores 90 on bench.",
        url="https://x.com/a", source_name="X Blog", source_type="rss",
        published_at=None, images=[], raw_summary=None,
        fetched_at=datetime.now(timezone.utc), topic="AI")


# ── Builtin registry ─────────────────────────────────────────────────────

def test_builtin_styles_count_and_default():
    styles = S.builtin_styles()
    assert len(styles) >= 8
    # deep-tech is first and is the default
    assert styles[0].id == S.DEFAULT_STYLE_ID == "deep-tech"
    assert styles[0].is_default is True
    # ids are unique and all builtin
    ids = [s.id for s in styles]
    assert len(ids) == len(set(ids))
    assert all(s.is_builtin for s in styles)


def test_every_builtin_instruction_has_content_placeholder():
    for s in S.builtin_styles():
        assert "{content}" in s.instruction, s.id
        assert s.prompt.strip(), s.id


def test_deep_tech_render_matches_legacy_format():
    """The default style must reproduce the old .format() output exactly so
    existing behaviour is unchanged."""
    dt = S.builtin_styles()[0]
    old = REWRITE_INSTRUCTION.format(
        title="T", source="SRC", manifest="MAN", content="CON")
    new = render_instruction(
        dt.instruction, title="T", source="SRC", manifest="MAN", content="CON")
    assert new == old
    assert dt.prompt == REWRITE_SYSTEM


def test_render_instruction_substitutes_all_placeholders():
    tmpl = "T={title} S={source} M={manifest} C={content}"
    out = render_instruction(tmpl, title="t", source="s", manifest="m", content="c")
    assert out == "T=t S=s M=m C=c"


# ── Custom style CRUD ─────────────────────────────────────────────────────

def test_create_custom_style_and_list():
    st = FakeStore()
    s = S.save_custom_style(
        st, id=None, name="硬核评测", description="desc",
        prompt="你是评测人", instruction="改写 {content}")
    assert s.id == "custom"  # slug of a non-ascii name falls back to 'custom'
    all_ids = [x.id for x in S.all_styles(st)]
    assert s.id in all_ids
    # persisted as JSON
    assert st.get_setting(S.CUSTOM_STYLES_KEY)
    data = json.loads(st.get_setting(S.CUSTOM_STYLES_KEY))
    assert data[0]["name"] == "硬核评测"


def test_create_requires_content_placeholder():
    st = FakeStore()
    with pytest.raises(ValueError):
        S.save_custom_style(st, id=None, name="x", description="",
                            prompt="", instruction="no placeholder here")


def test_create_requires_name():
    st = FakeStore()
    with pytest.raises(ValueError):
        S.save_custom_style(st, id=None, name="  ", description="",
                            prompt="", instruction="{content}")


def test_unique_ids_for_same_name():
    st = FakeStore()
    a = S.save_custom_style(st, id=None, name="My Voice", description="",
                            prompt="", instruction="{content}")
    b = S.save_custom_style(st, id=None, name="My Voice", description="",
                            prompt="", instruction="{content}")
    assert a.id != b.id
    assert a.id == "my-voice" and b.id == "my-voice-2"


def test_update_custom_style():
    st = FakeStore()
    a = S.save_custom_style(st, id=None, name="Voice", description="d1",
                            prompt="p1", instruction="{content}")
    S.save_custom_style(st, id=a.id, name="Voice2", description="d2",
                        prompt="p2", instruction="new {content}")
    got = S.get_style(a.id, st)
    assert got.name == "Voice2" and got.prompt == "p2"


def test_cannot_overwrite_or_delete_builtin():
    st = FakeStore()
    with pytest.raises(ValueError):
        S.save_custom_style(st, id="deep-tech", name="x", description="",
                            prompt="", instruction="{content}")
    with pytest.raises(ValueError):
        S.delete_custom_style(st, "deep-tech")


def test_delete_custom_style_resets_default():
    st = FakeStore()
    a = S.save_custom_style(st, id=None, name="Voice", description="",
                            prompt="", instruction="{content}")
    S.set_default_style(st, a.id)
    assert S.get_default_style_id(st) == a.id
    assert S.delete_custom_style(st, a.id) is True
    # default falls back to deep-tech after deletion
    assert S.get_default_style_id(st) == S.DEFAULT_STYLE_ID


def test_delete_missing_returns_false():
    st = FakeStore()
    assert S.delete_custom_style(st, "does-not-exist") is False


# ── Default resolution ────────────────────────────────────────────────────

def test_default_style_id_fallback():
    st = FakeStore()
    assert S.get_default_style_id(st) == S.DEFAULT_STYLE_ID
    # invalid stored id → fallback
    st.set_setting(S.DEFAULT_STYLE_KEY, "nonexistent")
    assert S.get_default_style_id(st) == S.DEFAULT_STYLE_ID


def test_set_default_to_builtin():
    st = FakeStore()
    S.set_default_style(st, "economist")
    assert S.get_default_style_id(st) == "economist"
    # setting back to deep-tech clears the setting
    S.set_default_style(st, "deep-tech")
    assert st.get_setting(S.DEFAULT_STYLE_KEY) is None


def test_set_default_invalid_raises():
    st = FakeStore()
    with pytest.raises(ValueError):
        S.set_default_style(st, "bogus-id")


def test_resolve_style_prefers_explicit_then_default():
    st = FakeStore()
    S.set_default_style(st, "economist")
    # explicit wins
    assert S.resolve_style("standup", st).id == "standup"
    # None → configured default
    assert S.resolve_style(None, st).id == "economist"
    # unknown id → default
    assert S.resolve_style("nope", st).id == "economist"


# ── rewrite() honours the chosen style ────────────────────────────────────

def test_rewrite_uses_style_prompt_and_instruction():
    style = S.get_style("economist")
    resp = json.dumps({"title_candidates": ["标题"], "body_md": "正文"})
    check = json.dumps({"flagged_claims": []})
    provider = RecordingProvider([resp, check])
    draft = rewrite(_article(), provider, style=style)
    assert draft.title_candidates[0] == "标题"
    # First call is the rewrite; verify the style's system prompt was used.
    sys_msg = provider.calls[0][0]
    user_msg = provider.calls[0][1]
    assert sys_msg.content == style.prompt
    assert sys_msg.content != REWRITE_SYSTEM
    # Article content is substituted into the instruction.
    assert "The model scores 90" in user_msg.content


def test_rewrite_without_style_matches_default_prompt():
    resp = json.dumps({"title_candidates": ["t"], "body_md": "b"})
    check = json.dumps({"flagged_claims": []})
    provider = RecordingProvider([resp, check])
    rewrite(_article(), provider)  # style=None
    assert provider.calls[0][0].content == REWRITE_SYSTEM
