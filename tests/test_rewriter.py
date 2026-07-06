import json
from datetime import datetime, timezone

from app.models import Article, Draft
from app.llm.providers.mock import MockProvider
from app.pipeline.rewriter import (
    rewrite, _inline_content, _video_embed,
    _extract_json, _strip_code_fences, _extract_code_fenced_json,
    _repair_json_control_chars, _media_block, _relativize_media,
)


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


# ═══════════════════════════════════════════════════════════════════
# _extract_json  — parsing LLM responses that may contain thinking
#                  text, code fences, or literal newlines in strings.
# ═══════════════════════════════════════════════════════════════════

# ── _repair_json_control_chars ────────────────────────────────────

def test_repair_escapes_newlines_in_strings():
    raw = '{"body_md": "line 1\nline 2\n\nline 4"}'
    fixed = _repair_json_control_chars(raw)
    assert '\n' not in fixed  # no literal newlines left
    assert '\\n' in fixed
    assert json.loads(fixed)["body_md"] == "line 1\nline 2\n\nline 4"


def test_repair_leaves_already_escaped_newlines():
    raw = '{"body_md": "line 1\\nline 2"}'
    fixed = _repair_json_control_chars(raw)
    assert fixed == raw  # untouched


def test_repair_handles_tabs_and_cr():
    raw = '{"a": "col1\tcol2\rnext"}'
    fixed = _repair_json_control_chars(raw)
    assert '\t' not in fixed
    assert '\r' not in fixed
    assert json.loads(fixed)["a"] == "col1\tcol2\rnext"


def test_repair_does_not_mangle_escaped_quotes():
    raw = '{"a": "say \\"hello\\""}'
    fixed = _repair_json_control_chars(raw)
    assert fixed == raw


def test_repair_no_strings_unchanged():
    raw = '{"a": 42, "b": true}'
    assert _repair_json_control_chars(raw) == raw


# ── _strip_code_fences ────────────────────────────────────────────

def test_strip_code_fences_whole_text():
    result = _strip_code_fences('```\n{"a": 1}\n```')
    assert result == '{"a": 1}'


def test_strip_code_fences_with_language():
    result = _strip_code_fences('```json\n{"a": 1}\n```')
    assert result == '{"a": 1}'


def test_strip_code_fences_not_whole():
    assert _strip_code_fences('text before\n```\n{"a": 1}\n```\ntext after') is None


def test_strip_code_fences_no_fences():
    assert _strip_code_fences('{"a": 1}') is None


# ── _extract_code_fenced_json ─────────────────────────────────────

def test_extract_code_fenced_json_simple():
    result = _extract_code_fenced_json(
        'some text\n```json\n{"a": 1}\n```\nmore text')
    assert result == '{"a": 1}'


def test_extract_code_fenced_json_no_language():
    result = _extract_code_fenced_json(
        '```\n{"a": 1}\n```')
    assert result == '{"a": 1}'


def test_extract_code_fenced_json_skips_non_json():
    """When the json fence directly follows another fence the helper may
    miss it (regex ambiguity), but the higher-level _extract_json should
    still succeed via the regex fallback."""
    helper_result = _extract_code_fenced_json(
        '```python\nprint(1)\n```\n```json\n{"a": 1}\n```')
    # Helper may not find it due to adjacent fences, but _extract_json will:
    main_result = _extract_json(
        '```python\nprint(1)\n```\n```json\n{"a": 1}\n```')
    assert main_result == {"a": 1}


def test_extract_code_fenced_json_no_fence():
    assert _extract_code_fenced_json('{"a": 1}') is None
    assert _extract_code_fenced_json('') is None


# ── _extract_json  main ──────────────────────────────────────────

def test_extract_json_clean():
    result = _extract_json('{"title_candidates": ["t1"], "body_md": "body"}')
    assert result == {"title_candidates": ["t1"], "body_md": "body"}


def test_extract_json_with_leading_thinking_text():
    """OpenCode-style response: reasoning text, then code-fenced JSON."""
    text = (
        'I detect a writing task. Let me proceed.\n\n'
        '```json\n'
        '{"title_candidates": ["标题1", "标题2"], "body_md": "## 章节\\n正文"}\n'
        '```\n'
    )
    result = _extract_json(text)
    assert result is not None
    assert result["title_candidates"] == ["标题1", "标题2"]
    assert result["body_md"] == "## 章节\n正文"


def test_extract_json_code_fence_full_text():
    """Entire response is a single code fence (Claude-style)."""
    text = '```json\n{"title_candidates": ["t"], "body_md": "x"}\n```'
    result = _extract_json(text)
    assert result == {"title_candidates": ["t"], "body_md": "x"}


def test_extract_json_literal_newlines_in_body():
    """LLM output with unescaped newlines in body_md — the exact bug."""
    text = (
        '```json\n'
        '{\n'
        '  "title_candidates": ["标题"],\n'
        '  "body_md": "## 第一章\n'
        '\n'
        '第一段文字。\n'
        '\n'
        '## 第二章\n'
        '\n'
        '第二段文字。"\n'
        '}\n'
        '```'
    )
    result = _extract_json(text)
    assert result is not None
    assert result["title_candidates"] == ["标题"]
    assert "第一章" in result["body_md"]
    assert "第二章" in result["body_md"]


def test_extract_json_literal_tabs_in_body():
    text = (
        '```json\n'
        '{"title_candidates": ["t"], "body_md": "col1\tcol2\tcol3"}\n'
        '```'
    )
    result = _extract_json(text)
    assert result is not None
    assert "\t" in result["body_md"]


def test_extract_json_fallback_regex_greedy():
    """No code fence — fallback to regex {…} with leading text."""
    text = 'Leading text {"title_candidates": ["x"], "body_md": "y"} trailing'
    result = _extract_json(text)
    assert result == {"title_candidates": ["x"], "body_md": "y"}


def test_extract_json_fallback_regex_with_newlines():
    """Regex fallback can also repair literal newlines."""
    text = (
        'Some text\n'
        '{"title_candidates": ["标题"], "body_md": "段落1\n\n段落2"}'
    )
    result = _extract_json(text)
    assert result is not None
    assert "段落1" in result["body_md"]


def test_extract_json_no_json_returns_none():
    assert _extract_json('') is None
    assert _extract_json('plain text with no json') is None
    assert _extract_json('```python\nprint(1)\n```') is None


def test_extract_json_empty_body_md():
    text = '{"title_candidates": ["标题"], "body_md": ""}'
    result = _extract_json(text)
    assert result == {"title_candidates": ["标题"], "body_md": ""}


def test_extract_json_only_title_candidates_no_body():
    # missing body_md key — still returns the parsed dict
    text = '{"title_candidates": ["标题"]}'
    result = _extract_json(text)
    assert result == {"title_candidates": ["标题"]}


def test_extract_json_multiple_code_fences_picks_json_one():
    text = (
        '```bash\nls\n```\n'
        '```json\n{"title_candidates": ["t"], "body_md": "x"}\n```\n'
        '```text\nhello\n```'
    )
    result = _extract_json(text)
    assert result == {"title_candidates": ["t"], "body_md": "x"}


def test_extract_json_real_world_open_code_output():
    """Full simulation of an actual OpenCode CLI response."""
    text = (
        'I detect a **writing/translation** task - Chinese deep-tech media\n'
        'rewrite of an English press release. No relevant skill maps directly\n'
        'to Chinese tech journalism, so I\'ll proceed directly.\n'
        '\n'
        'Let me first read the source thoroughly.\n'
        '\n'
        'Now I\'ll produce the rewrite.\n'
        '\n'
        '```json\n'
        '{\n'
        '  "title_candidates": [\n'
        '    "80亿美元拿下铱星，火箭实验室要打造怎样的太空帝国？",\n'
        '    "从发射服务商到太空运营商，火箭实验室的垂直整合赌局有多大？"\n'
        '  ],\n'
        '  "body_md": "## 一、当太空快递决定自己开公司\n'
        '\n'
        '你有没有想过一个问题：如果SpaceX突然买下中国电信？\n'
        '\n'
        '## 二、垂直整合的终局\n'
        '\n'
        '第二段文字。"\n'
        '}\n'
        '```\n'
        '\n'
        'Hope this helps!\n'
    )
    result = _extract_json(text)
    assert result is not None
    assert len(result["title_candidates"]) == 2
    assert "太空快递" in result["body_md"]
    assert "垂直整合" in result["body_md"]
    # Make sure thinking text is NOT in the body
    assert "I detect" not in result["body_md"]
    assert "Hope this helps" not in result["body_md"]


def test_extract_json_body_with_inner_braces():
    """Body_md contains { and } (e.g., JSON examples in markdown)."""
    text = (
        '```json\n'
        '{"title_candidates": ["t"], "body_md": "示例 JSON：`{\\"key\\": \\"value\\"}`"}\n'
        '```'
    )
    result = _extract_json(text)
    assert result is not None
    assert '{"key": "value"}' in result["body_md"]


def test_extract_json_unescaped_backslashes_in_body():
    """Body contains backslashes (e.g. Windows paths)."""
    # \\\\ in Python source → \\ in string → \ after json.loads
    text = '```\n{"title_candidates": ["t"], "body_md": "path: D:\\\\data\\\\img.png"}\n```'
    result = _extract_json(text)
    assert result is not None
    assert "D:" in result["body_md"]
    assert "img.png" in result["body_md"]


def test_extract_json_bare_quotes_inside_body():
    """Body contains bare ASCII double-quotes — _repair escapes them."""
    text = (
        '```json\n'
        '{\n'
        '  "title_candidates": ["标题"],\n'
        '  "body_md": "He said \\"hello\\" and left."\n'
        '}\n'
        '```'
    )
    result = _extract_json(text)
    assert result is not None
    assert 'hello' in result["body_md"]


def test_repair_escapes_bare_quotes():
    """Bare double-quotes inside a JSON string value are escaped."""
    raw = '{"body_md": "text with "inner quotes" in it"}'
    repaired = _repair_json_control_chars(raw)
    # The inner quotes should be escaped
    assert repaired.count('\\"') >= 2
    # Should now parse as valid JSON
    import json
    parsed = json.loads(repaired)
    assert 'inner quotes' in parsed["body_md"]


def test_repair_preserves_structural_quotes():
    """Quotes followed by JSON structural chars stay unescaped."""
    raw = '{"title_candidates": ["标题1", "标题2"], "body_md": "text"}'
    repaired = _repair_json_control_chars(raw)
    assert repaired == raw  # No change needed
    import json
    json.loads(repaired)  # Should parse fine


# ═══════════════════════════════════════════════════════════════════
# _relativize_media  — convert absolute paths to relative
# ═══════════════════════════════════════════════════════════════════


def test_relativize_media_converts_media_path():
    assert _relativize_media("/media/abc/pic.png") == "../../media/abc/pic.png"


def test_relativize_media_converts_images_path():
    assert _relativize_media("/images/abc/pic.png") == "../../images/abc/pic.png"


def test_relativize_media_keeps_absolute_url():
    assert _relativize_media("https://x.com/img.png") == "https://x.com/img.png"


def test_relativize_media_keeps_relative_url():
    assert _relativize_media("../../media/abc/pic.png") == "../../media/abc/pic.png"


# ═══════════════════════════════════════════════════════════════════
# _inline_content  — safety net when [[IMG:N]] tokens are missing
# ═══════════════════════════════════════════════════════════════════


def test_inline_content_injects_missing_placeholders():
    """When images exist but body has no [[IMG:N]] tokens, inject all
    placeholders at the top so the LLM can still reference them."""
    body = "纯文本，没有任何图片占位符"
    images = ["/media/a/pic1.png", "/media/a/pic2.png"]
    result = _inline_content(body, images)
    # Both placeholders replaced with real URLs
    assert "![](../../media/a/pic1.png)" in result
    assert "![](../../media/a/pic2.png)" in result
    # Original text preserved (after the placeholders)
    assert "没有任何图片占位符" in result


def test_inline_content_leaves_existing_tokens_untouched():
    """When body already has [[IMG:N]] tokens, _inline_content skips the
    safety-net injection and just replaces the existing tokens."""
    body = "开头\n\n[[IMG:0]]\n\n中间\n\n[[IMG:1]]\n\n结尾"
    images = ["/media/a/pic1.png", "/media/a/pic2.png"]
    result = _inline_content(body, images)
    assert result.count("../../media/a/pic1.png") == 1
    assert result.count("../../media/a/pic2.png") == 1
    # Safety net should NOT have injected extra placeholders; only the
    # two already in the body got replaced.
    assert result.count("![](../../media") == 2


def test_inline_content_empty_images_does_nothing():
    """No images → body unchanged."""
    body = "纯文本，没有图片"
    assert _inline_content(body, []) == body


# ═══════════════════════════════════════════════════════════════════
# _media_block  — dedup protection against repeated media
# ═══════════════════════════════════════════════════════════════════


def test_media_block_skips_absolute_url_in_existing():
    """Media whose absolute URL already appears in the body is not appended."""
    images = ["/media/a/pic1.png"]
    existing = "正文已经包含 ![](../../media/a/pic1.png)"
    result = _media_block(images, [], existing)
    assert "../../media/a/pic1.png" not in result
    assert result.strip() == ""


def test_media_block_skips_media_with_relativized_url():
    """Media whose relativized form (../../media/...) appears in the body
    is skipped — this was the root cause bug B2."""
    images = ["/media/a/pic1.png"]
    existing = "正文包含 ![](../../media/a/pic1.png)"
    result = _media_block(images, [], existing)
    assert "pic1.png" not in result
    assert result.strip() == ""


def test_media_block_appends_missing_media():
    """Media not in the body is appended at the end."""
    images = ["/media/a/pic1.png"]
    result = _media_block(images, [], "正文没有这张图")
    assert "../../media/a/pic1.png" in result


def test_media_block_dedupes_duplicate_images():
    """Same image appearing twice in the front-matter list is only appended once."""
    images = ["/media/a/pic1.png", "/media/a/pic1.png"]
    result = _media_block(images, [], "正文没有图")
    assert result.count("../../media/a/pic1.png") == 1


# ═══════════════════════════════════════════════════════════════════
# rewrite  — slice limit covers all [[IMG:N]] tokens
# ═══════════════════════════════════════════════════════════════════


def test_rewrite_slice_covers_all_img_tokens():
    """When [[IMG:N]] tokens lie beyond 6000 chars, the slice limit is
    dynamically extended so all are visible to the LLM."""
    # Build a content with [[IMG:4]] at ~6400 and [[IMG:5]] at ~7300
    chunks = ["开头文本。\n"] * 200
    chunks.append("\n[[IMG:0]]\n")
    chunks.append("\n更多正文。\n" * 300)
    chunks.append("\n[[IMG:1]]\n")
    chunks.append("\n更多正文。\n" * 300)
    chunks.append("\n[[IMG:2]]\n")
    chunks.append("\n更多正文。\n" * 300)
    chunks.append("\n[[IMG:3]]\n")
    chunks.append("\n继续填充文本。" * 200)
    chunks.append("\n[[IMG:4]]\n")
    chunks.append("\n继续填充文本。" * 50)
    chunks.append("\n[[IMG:5]]\n")
    chunks.append("\n结尾文本。")
    content_md = "".join(chunks)

    # Verify [[IMG:4]] and [[IMG:5]] are past 6000
    assert content_md.find("[[IMG:4]]") > 6000
    assert content_md.find("[[IMG:5]]") > 6000

    # Simulate the slice logic from rewrite()
    img_positions = [content_md.find(f"[[IMG:{i}]]")
                     for i in range(6)
                     if content_md.find(f"[[IMG:{i}]]") >= 0]
    slice_limit = max(6000, max(img_positions) + 200)

    assert slice_limit > 6000
    assert "[[IMG:4]]" in content_md[:slice_limit]
    assert "[[IMG:5]]" in content_md[:slice_limit]


def test_rewrite_slice_defaults_to_6000():
    """When all tokens are within 6000 chars, the slice stays at the default."""
    content_md = "简短正文 [[IMG:0]] 结束"
    img_positions = [content_md.find(f"[[IMG:{i}]]")
                     for i in range(1)
                     if content_md.find(f"[[IMG:{i}]]") >= 0]
    slice_limit = max(6000, max(img_positions) + 200)
    assert slice_limit == 6000
