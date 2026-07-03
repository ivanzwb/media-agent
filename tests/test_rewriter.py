import json
from datetime import datetime, timezone

from app.models import Article, Draft
from app.llm.providers.mock import MockProvider
from app.pipeline.rewriter import rewrite, _inline_content, _video_embed


def sample_article():
    return Article(title="New AI Model", content_md="The model scores 90 on bench.",
                   url="https://x.com/a", source_name="X Blog", source_type="rss",
                   published_at=None, images=["https://x.com/img.png"],
                   raw_summary=None, fetched_at=datetime.now(timezone.utc),
                   topic="AI")


def test_rewrite_builds_draft_from_json():
    rewrite_json = json.dumps({
        "title_candidates": ["震撼！新模型登场", "新AI模型刷新纪录"],
        "body_md": "## 开头钩子\n正文内容\n",
    })
    check_json = json.dumps({"flagged_claims": []})
    provider = MockProvider(responses=[rewrite_json, check_json])
    draft = rewrite(sample_article(), provider)
    assert isinstance(draft, Draft)
    assert draft.title_candidates[0] == "震撼！新模型登场"
    assert "正文内容" in draft.body_md
    assert draft.source_url == "https://x.com/a"
    assert draft.topic == "AI"
    assert draft.flagged_claims == []


def test_rewrite_captures_flagged_claims():
    rewrite_json = json.dumps({
        "title_candidates": ["标题"],
        "body_md": "正文 with 99% number not in source",
    })
    check_json = json.dumps({
        "flagged_claims": ["'99%' is not supported by the source"]})
    provider = MockProvider(responses=[rewrite_json, check_json])
    draft = rewrite(sample_article(), provider)
    assert len(draft.flagged_claims) == 1
    assert "99%" in draft.flagged_claims[0]


def test_rewrite_handles_non_json_gracefully():
    provider = MockProvider(responses=["not json at all", "also not json"])
    draft = rewrite(sample_article(), provider)
    assert draft.title_candidates
    assert draft.body_md


def test_rewrite_preserves_images_and_videos():
    art = Article(
        title="New AI Model", content_md="正文",
        url="https://x.com/a", source_name="X Blog", source_type="scrape",
        published_at=None, images=["https://x.com/pic1.png"],
        raw_summary=None, fetched_at=datetime.now(timezone.utc), topic="AI",
        videos=["https://www.youtube.com/embed/abc123"])
    rewrite_json = json.dumps({
        "title_candidates": ["标题"], "body_md": "## 改写正文\n不含图片"})
    provider = MockProvider(responses=[rewrite_json, json.dumps({"flagged_claims": []})])
    draft = rewrite(art, provider)
    assert "https://x.com/pic1.png" in draft.body_md
    assert "配图" not in draft.body_md  # removed in favour of bare image appending
    assert "youtube.com/embed/abc123" in draft.body_md
    assert "视频（来自原文）" in draft.body_md


def test_rewrite_keeps_inlined_images_in_position():
    """With _inline_content, the LLM sees real `![](url)` in the source and
    should place them in the body at natural positions — no placeholders."""
    art = Article(
        title="t", content_md="c", url="https://x.com/a", source_name="X",
        source_type="scrape", published_at=None,
        images=["https://x.com/pic1.png", "https://x.com/pic2.png"],
        raw_summary=None, fetched_at=datetime.now(timezone.utc), topic="AI",
        videos=["https://www.youtube.com/embed/abc"])
    # LLM places images naturally with ![](url) syntax
    body = (
        "## 段落一\n讲了背景\n\n"
        "![](https://x.com/pic1.png)\n\n"
        "## 段落二\n演示视频：\n\n"
    )
    rewrite_json = json.dumps({"title_candidates": ["标题"], "body_md": body})
    provider = MockProvider(responses=[rewrite_json, json.dumps({"flagged_claims": []})])
    draft = rewrite(art, provider)
    b = draft.body_md
    # pic1 placed before 段落二 by LLM
    assert "![](https://x.com/pic1.png)" in b
    assert b.index("pic1.png") < b.index("段落二")
    # video appended by _media_block since LLM didn't include it
    assert "youtube.com/embed/abc" in b
    # pic2 appended as unused
    assert "pic2.png" in b


def test_inline_content_handles_videos():
    """_inline_content replaces [[VIDEO:N]] with real <iframe> embeds."""
    source = "text before\n\n[[VIDEO:0]]\n\ntext after"
    videos = ["https://youtube.com/embed/xyz"]
    result = _inline_content(source, images=[], videos=videos)
    assert "[[VIDEO:0]]" not in result
    assert '<iframe' in result
    assert 'youtube.com/embed/xyz' in result
    assert result.index("text before") < result.index("<iframe") < result.index("text after")


def test_rewrite_skips_media_already_in_body():
    art = Article(
        title="t", content_md="c", url="https://x.com/a", source_name="X",
        source_type="scrape", published_at=None,
        images=["https://x.com/pic1.png"], raw_summary=None,
        fetched_at=datetime.now(timezone.utc), topic="AI")
    # body already contains the image -> should not be appended again
    body = "正文 ![](https://x.com/pic1.png)"
    rewrite_json = json.dumps({"title_candidates": ["标题"], "body_md": body})
    provider = MockProvider(responses=[rewrite_json, json.dumps({"flagged_claims": []})])
    draft = rewrite(art, provider)
    assert draft.body_md.count("https://x.com/pic1.png") == 1
    assert "配图（来自原文）" not in draft.body_md
