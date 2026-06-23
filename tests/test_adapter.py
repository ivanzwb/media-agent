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
