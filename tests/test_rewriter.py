import json
from datetime import datetime, timezone

from app.models import Article, Draft
from app.llm.providers.mock import MockProvider
from app.pipeline.rewriter import rewrite


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
