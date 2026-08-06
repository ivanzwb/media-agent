import json

import pytest

from app.llm.providers.mock import MockProvider
from app.pipeline.adapter import adapt, PLATFORMS


def test_platforms_present():
    assert {"wechat", "xiaohongshu", "zhihu"} <= set(PLATFORMS)


def test_adapt_parses_json():
    provider = MockProvider(responses=[json.dumps({
        "title_candidates": ["小红书爆款标题✨", "另一个"],
        "body_md": "正文 #AI #科技"})])
    out = adapt("主稿正文事实", "原标题", "xiaohongshu", provider)
    assert out["title_candidates"][0] == "小红书爆款标题✨"
    assert "#AI" in out["body_md"]


def test_adapt_fallback_on_non_json():
    provider = MockProvider(responses=["不是json"])
    out = adapt("主稿", "原标题", "wechat", provider)
    assert out["title_candidates"] == ["原标题"]
    assert out["body_md"] == "不是json"


def test_adapt_unknown_platform_raises():
    with pytest.raises(ValueError):
        adapt("m", "t", "tiktok", MockProvider())


class _Spy(MockProvider):
    def __init__(self, responses):
        super().__init__(responses=responses)
        self.systems: list[str] = []

    def chat(self, messages, **opts):
        self.systems += [m.content for m in messages if m.role == "system"]
        return super().chat(messages, **opts)


@pytest.mark.parametrize("platform", sorted(PLATFORMS))
def test_every_platform_adapts_under_the_anti_slop_rules(platform):
    """The adapted copy is the one that actually gets published, so this is
    the last place the rules can still reach the text. Parametrised so a newly
    registered platform cannot quietly opt out."""
    provider = _Spy([json.dumps({"title_candidates": ["题"],
                                 "body_md": "正文"})])
    adapt("主稿", "原标题", platform, provider)
    assert provider.systems and "严禁 AI 腔" in provider.systems[0]


def test_the_adapted_body_is_cleaned_like_the_master_draft():
    provider = MockProvider(responses=[json.dumps({
        "title_candidates": ["题"],
        "body_md": "近年来，这件事深入浅出。"})])
    out = adapt("主稿", "原标题", "wechat", provider)
    assert "近年来" not in out["body_md"]
    assert "深入浅出" not in out["body_md"]


def test_a_non_json_reply_is_cleaned_too():
    """The fallback path publishes the raw reply, so skipping cleanup there
    would let slop through exactly when the model was already misbehaving."""
    provider = MockProvider(responses=["近年来，深入浅出的解读。"])
    out = adapt("主稿", "原标题", "wechat", provider)
    assert "近年来" not in out["body_md"]
