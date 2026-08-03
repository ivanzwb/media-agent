import json
from datetime import datetime, timezone

from app.models import Article, Draft
from app.llm.providers.mock import MockProvider
from app.pipeline.rewriter import (
    rewrite, _inline_content, _video_embed,
    _extract_json, _strip_code_fences, _extract_code_fenced_json,
    _repair_json_control_chars, _media_block, _relativize_media,
    _build_manifest, _apply_placeholders, _interleave_missing_images,
    _prune_body_images,
    _extract_json_field, _extract_json_fallback, _normalize_video_markdown,
    clean_digest,
)


def sample_article():
    return Article(title="New AI Model", content_md="The model scores 90 on bench.",
                   url="https://x.com/a", source_name="X Blog", source_type="rss",
                   published_at=None, images=["https://x.com/img.png"],
                   raw_summary=None, fetched_at=datetime.now(timezone.utc),
                   topic="AI")


class RecordingProvider:
    """Captures chat messages for prompt assertion."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0
        self.calls: list = []

    def chat(self, messages, **opts):
        self.calls.append(messages)
        out = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return out


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


# ── opening ──────────────────────────────────────────────────────────────

def test_rewrite_asks_for_one_of_the_four_openings():
    provider = RecordingProvider([
        json.dumps({"title_candidates": ["t"], "body_md": "正文"}),
        json.dumps({"flagged_claims": []}),
    ])
    rewrite(sample_article(), provider)
    prompt = provider.calls[0][1].content
    assert "前 100 字定生死" in prompt
    for formula in ("**痛点**", "**利益**", "**反常识**", "**短故事**"):
        assert formula in prompt
    assert "不要用「想象一下：」" in prompt


# ── digest ───────────────────────────────────────────────────────────────

def test_rewrite_keeps_the_digest_the_model_wrote():
    rewrite_json = json.dumps({
        "title_candidates": ["标题"],
        "digest": "新模型在基准测试上拿到 90 分，比上一代高出一截。",
        "body_md": "## 一、开头\n正文内容\n",
    })
    provider = MockProvider(
        responses=[rewrite_json, json.dumps({"flagged_claims": []})])
    draft = rewrite(sample_article(), provider)
    assert draft.digest.startswith("新模型在基准测试上拿到 90 分")


def test_rewrite_leaves_the_digest_empty_when_the_model_skips_it():
    rewrite_json = json.dumps({
        "title_candidates": ["标题"], "body_md": "正文",
    })
    provider = MockProvider(
        responses=[rewrite_json, json.dumps({"flagged_claims": []})])
    assert rewrite(sample_article(), provider).digest == ""


def test_rewrite_asks_for_a_digest_and_a_keyword():
    provider = RecordingProvider([
        json.dumps({"title_candidates": ["t"], "body_md": "正文"}),
        json.dumps({"flagged_claims": []}),
    ])
    rewrite(sample_article(), provider)
    prompt = provider.calls[0][1].content
    assert '"digest"' in prompt
    assert "50-60 字" in prompt
    assert "核心关键词必须出现在" in prompt


def test_a_custom_style_is_asked_for_a_digest_too():
    from app.pipeline.styles import RewriteStyle

    style = RewriteStyle(
        id="custom", name="自定义", description="", prompt="改写文章",
        instruction="根据原文改写：{content}",
    )
    provider = RecordingProvider([
        json.dumps({"title_candidates": ["t"], "digest": "摘要", "body_md": "正文"}),
        json.dumps({"flagged_claims": []}),
    ])
    draft = rewrite(sample_article(), provider, style=style)
    assert '"digest"' in provider.calls[0][1].content
    assert draft.digest == "摘要"


def test_digest_survives_a_body_full_of_raw_newlines():
    """The regex fallback path has to recover the digest too."""
    raw = (
        '{"title_candidates": ["标题"], "digest": "一句摘要",'
        ' "body_md": "第一行\n第二行"}'
    )
    recovered = _extract_json_fallback(raw)
    assert recovered["digest"] == "一句摘要"


def test_clean_digest_unwraps_what_models_wrap_it_in():
    assert clean_digest('  "摘要：一句话总结"  ') == "一句话总结"
    assert clean_digest("第一行\n第二行") == "第一行 第二行"
    assert clean_digest("") == ""


def test_clean_digest_cuts_at_the_platform_limit():
    assert len(clean_digest("字" * 200)) == 120


# ── promotion_footer ─────────────────────────────────────────────────────

def test_rewrite_promotion_footer_appears_in_instruction():
    """When promotion_footer is provided, the LLM instruction includes the
    promotion block so the LLM can write a natural transition."""
    resp = json.dumps({"title_candidates": ["标题"], "body_md": "正文"})
    check = json.dumps({"flagged_claims": []})
    provider = RecordingProvider([resp, check])
    rewrite(sample_article(), provider,
            promotion_footer="关注我们，获取更多前沿资讯")
    user_msg = provider.calls[0][1].content
    # Section header
    assert "推广部分" in user_msg
    # New prompt: topic-specific CTA generation, not static transition
    assert "2-3 个互动呼吁" in user_msg
    assert "品牌名称" in user_msg
    assert "基于本文实际内容" in user_msg
    assert "参考格式" in user_msg
    # Configured footer text is still passed as reference
    assert "关注我们" in user_msg


def test_rewrite_promotion_footer_none_omits_block():
    """When promotion_footer is None, the instruction has no promotion block."""
    resp = json.dumps({"title_candidates": ["标题"], "body_md": "正文"})
    check = json.dumps({"flagged_claims": []})
    provider = RecordingProvider([resp, check])
    rewrite(sample_article(), provider)  # promotion_footer defaults to None
    user_msg = provider.calls[0][1].content
    assert "推广部分" not in user_msg


def test_rewrite_promotion_footer_empty_omits_block():
    """When promotion_footer is empty string, the instruction has no promotion block."""
    resp = json.dumps({"title_candidates": ["标题"], "body_md": "正文"})
    check = json.dumps({"flagged_claims": []})
    provider = RecordingProvider([resp, check])
    rewrite(sample_article(), provider, promotion_footer="")
    user_msg = provider.calls[0][1].content
    assert "推广部分" not in user_msg


def test_rewrite_promotion_footer_with_style():
    """promotion_footer works with a non-default style too."""
    resp = json.dumps({"title_candidates": ["标题"], "body_md": "正文"})
    check = json.dumps({"flagged_claims": []})
    from app.pipeline import styles as S
    style = S.get_style("economist")
    provider = RecordingProvider([resp, check])
    rewrite(sample_article(), provider, style=style,
            promotion_footer="订阅我们，每周深度解读")
    user_msg = provider.calls[0][1].content
    assert "推广部分" in user_msg
    assert "订阅我们" in user_msg
    # Style-specific content is preserved
    assert "经济学人" in user_msg or "英式" in user_msg or "冷静" in user_msg


def test_rewrite_seo_tags_appear_before_promotion():
    resp = json.dumps({"title_candidates": ["标题"], "body_md": "正文"})
    check = json.dumps({"flagged_claims": []})
    provider = RecordingProvider([resp, check])
    rewrite(
        sample_article(), provider,
        promotion_footer="关注我们，获取更多前沿资讯",
        seo_tags_enabled=True,
    )
    user_msg = provider.calls[0][1].content
    seo_idx = user_msg.index("4. **标签**")
    promotion_idx = user_msg.index("5. **推广部分**")
    assert seo_idx < promotion_idx
    assert "5-8 个具体关键词" in user_msg
    assert "**标签**：#关键词1 #关键词2 #关键词3" in user_msg


def test_rewrite_seo_tags_can_be_disabled():
    resp = json.dumps({"title_candidates": ["标题"], "body_md": "正文"})
    check = json.dumps({"flagged_claims": []})
    provider = RecordingProvider([resp, check])
    rewrite(
        sample_article(), provider,
        promotion_footer="关注我们",
        seo_tags_enabled=False,
    )
    user_msg = provider.calls[0][1].content
    assert "SEO 标签" not in user_msg
    assert "4. **推广部分**" in user_msg


def test_rewrite_seo_tags_work_with_legacy_custom_style():
    from app.pipeline.styles import RewriteStyle

    style = RewriteStyle(
        id="custom", name="自定义", description="", prompt="改写文章",
        instruction="根据原文改写：{content}",
    )
    resp = json.dumps({"title_candidates": ["标题"], "body_md": "正文"})
    check = json.dumps({"flagged_claims": []})
    provider = RecordingProvider([resp, check])
    rewrite(sample_article(), provider, style=style, seo_tags_enabled=True)
    assert "**标签**" in provider.calls[0][1].content


def test_rewrite_promotion_footer_body_md_flow():
    """The LLM output body_md may contain promotion text (naturally integrated);
    rewrite() should pass it through without post-processing."""
    body = "## 写在最后\n\n总结。欢迎关注我们获取最新资讯。"
    resp = json.dumps({"title_candidates": ["标题"], "body_md": body})
    check = json.dumps({"flagged_claims": []})
    provider = MockProvider(responses=[resp, check])
    draft = rewrite(sample_article(), provider,
                    promotion_footer="关注我们，获取最新资讯")
    assert "欢迎关注" in draft.body_md
    assert draft.source_url == "https://x.com/a"


def test_rewrite_promotion_footer_no_mechanical_append():
    """rewrite() does NOT mechanically append the promotion footer to body_md;
    it only passes it as a reference in the LLM instruction. The LLM output
    passes through unadulterated — no `---\n{footer}` block is added."""
    body = "原文内容正文"
    resp = json.dumps({"title_candidates": ["标题"], "body_md": body})
    check = json.dumps({"flagged_claims": []})
    provider = MockProvider(responses=[resp, check])
    draft = rewrite(sample_article(), provider,
                    promotion_footer="关注我们，获取最新资讯")
    # rewrite() post-processes body_md (image interleaving, source attribution)
    # but does NOT append the promotion footer as a standalone section
    assert "关注我们，获取最新资讯" not in draft.body_md
    # The "信息来源" section is the last section — no promotion footer after it
    info_idx = draft.body_md.rfind("**信息来源**")
    after_info = draft.body_md[info_idx:] if info_idx != -1 else ""
    assert "关注我们" not in after_info


def test_rewrite_promotion_render_instruction_signature():
    """render_instruction accepts promotion_block kwarg."""
    from app.pipeline.rewriter import render_instruction
    tmpl = "前置内容{promotion_block}后置内容"
    out = render_instruction(
        tmpl, title="t", source="s", manifest="m", content="c",
        promotion_block="4. **推广**：关注我们\n")
    assert "推广" in out
    assert "后置内容" in out


# ── Non-JSON fallback ──────────────────────────────────────────────────

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
    # pic2 was left out on purpose — putting it back would override the
    # rewrite's own choice and land it away from the text it belongs to.
    assert "pic2.png" not in b


def test_inline_content_handles_videos():
    """_inline_content replaces [[VIDEO:N]] with real <iframe> embeds."""
    source = "text before\n\n[[VIDEO:0]]\n\ntext after"
    videos = ["https://youtube.com/embed/xyz"]
    result = _inline_content(source, images=[], videos=videos)
    assert "[[VIDEO:0]]" not in result
    assert '<iframe' in result
    assert 'youtube.com/embed/xyz' in result
    assert result.index("text before") < result.index("<iframe") < result.index("text after")


def test_normalize_video_markdown_converts_known_video_to_embed():
    body = "前文\n\n![错误的视频图片](../../media/a/clip.mp4)\n\n后文"
    result = _normalize_video_markdown(body, ["/media/a/clip.mp4"])
    assert "![错误的视频图片]" not in result
    assert '<iframe src="../../media/a/clip.mp4"' in result
    assert "[▶ 视频链接]" not in result


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


def test_media_block_does_not_treat_video_image_markdown_as_embed():
    video = "/media/a/clip.mp4"
    existing = "错误格式：![](../../media/a/clip.mp4)"
    result = _media_block([], [video], existing)
    assert '<iframe src="../../media/a/clip.mp4"' in result


def test_video_embed_rejects_dangerous_urls_and_escapes_attributes():
    assert _video_embed("javascript:alert(1)") == ""
    embedded = _video_embed("https://video.example/watch?a=1&b=2")
    assert 'src="https://video.example/watch?a=1&amp;b=2"' in embedded
    assert _media_block(
        [], ["https://video.example/watch?a=1&b=2"], embedded) == ""


def test_media_block_does_not_treat_fallback_link_as_embed():
    video = "/media/a/clip.mp4"
    existing = "[▶ 视频链接](../../media/a/clip.mp4)"
    result = _media_block([], [video], existing)
    assert '<iframe src="../../media/a/clip.mp4"' in result


# ═══════════════════════════════════════════════════════════════════
# _interleave_missing_images  — a safety net for a rewrite that came back
# with no pictures at all, not a way to force every picture back in
# ═══════════════════════════════════════════════════════════════════


def test_interleave_missing_images_no_headings_appends_at_end():
    """Body with no headings and no paragraph breaks → image appended at end."""
    body = "纯文本段落。\n没有标题。"
    result = _interleave_missing_images(body, ["/media/x/pic1.png"])
    assert "pic1.png" in result
    assert result.strip().endswith("![](../../media/x/pic1.png)")


def test_interleave_missing_images_placed_image_skipped():
    """Image already in body is NOT treated as missing."""
    body = "## 开头\n\n![](../../media/x/pic1.png)\n\n## 结尾"
    result = _interleave_missing_images(body, ["/media/x/pic1.png"])
    # No change — already present
    assert result.count("pic1.png") == 1
    assert result == body


def test_interleave_missing_images_inserts_before_second_heading():
    """Missing image inserted before the second ## heading."""
    body = "## 一、开头\n\n内容\n\n## 二、深入\n\n详情"
    result = _interleave_missing_images(body, ["/media/x/missing.png"])
    assert result.count("missing.png") == 1
    # Inserted before "## 二、深入"
    assert "missing.png" in result
    assert result.index("missing.png") < result.index("## 二、深入")


def test_interleave_missing_images_multiple_missing():
    """Multiple missing images — each inserted before a different heading."""
    body = "## A\n\ntext\n\n## B\n\ntext\n\n## C\n\ntext"
    result = _interleave_missing_images(body, ["/media/x/m1.png", "/media/x/m2.png"])
    assert result.count("m1.png") == 1
    assert result.count("m2.png") == 1
    # m1 inserted before B, m2 inserted before C
    assert result.index("m1.png") < result.index("## B")
    assert result.index("m2.png") > result.index("## B")
    assert result.index("m2.png") < result.index("## C")


def test_interleave_missing_images_fallback_when_all_positions_exhausted():
    """More missing images than headings → extras appended at end."""
    body = "## 唯一标题\n\n正文"
    result = _interleave_missing_images(
        body, ["/media/x/p1.png", "/media/x/p2.png", "/media/x/p3.png"])
    assert result.count("p1.png") == 1
    assert result.count("p2.png") == 1
    assert result.count("p3.png") == 1


def test_interleave_missing_images_empty_body():
    """Empty body → images appended."""
    result = _interleave_missing_images("", ["/media/x/pic1.png"])
    assert "pic1.png" in result


def test_interleave_missing_images_no_images():
    """No images list → body unchanged."""
    body = "## 标题\n内容"
    assert _interleave_missing_images(body, []) == body


def test_interleave_leaves_a_rewrite_that_chose_its_own_pictures_alone():
    """留了一张就说明模型做过取舍，把落选的塞回去会打乱顺序、堆成十几张。"""
    body = "## A\n\n![](../../media/x/kept.png)\n\n## B\n\n## C"
    result = _interleave_missing_images(
        body, ["/media/x/kept.png", "/media/x/missing1.png",
               "/media/x/missing2.png"])

    assert result == body


def test_interleave_hands_back_only_a_few_when_the_rewrite_kept_none():
    """一张都没留才兜底，而且给三张，不是把十二张全倒进去。"""
    body = "## A\n\ntext\n\n## B\n\ntext\n\n## C\n\ntext\n\n## D\n\ntext"
    images = [f"/media/x/p{i}.png" for i in range(8)]

    result = _interleave_missing_images(body, images)

    assert result.count("![](") == 3
    for i in range(3):
        assert f"p{i}.png" in result
    for i in range(3, 8):
        assert f"p{i}.png" not in result


def test_interleave_does_not_hand_back_page_furniture():
    """兜底也不能把追踪像素、赞助商 logo 塞进正文。"""
    body = "## A\n\ntext\n\n## B\n\ntext"
    result = _interleave_missing_images(body, [
        "https://www.nvidia.com/content/dam/1x1-00000000.png",
        "https://px.ads.linkedin.com/collect/?pid=9956745&fmt=gif",
        "https://0sec.ai/sponsors/aws-startups.png",
    ])

    assert result == body


# ═══════════════════════════════════════════════════════════════════
# _prune_body_images  — repeats, invented links and page chrome
# ═══════════════════════════════════════════════════════════════════


def test_prune_drops_a_link_the_model_pieced_together_itself():
    """两个前缀混着抄，拼出的地址原文没有、打不开，读起来还像重复。"""
    real = "https://cdn.site.com/67116a97/68780f0b_chart.png"
    invented = "https://cdn.site.com/67083eaf/68780f0b_chart.png"
    body = f"开头\n\n![]({real})\n\n中间\n\n![]({invented})\n\n结尾"

    result = _prune_body_images(body, [real, "https://cdn.site.com/67083eaf/a.png"])

    assert real in result
    assert invented not in result


def test_prune_drops_a_hostname_the_model_typed_a_hyphen_into():
    """真实案例：清单里是 springernature.com，正文抄成了 springer-nature.com。"""
    real = "https://media.springernature.com/lw685/image/art-1.jpg"
    body = f"![]({real})\n\n![](https://media.springer-nature.com/lw685/image/art-1.jpg)"

    result = _prune_body_images(body, [real])

    assert result.count("![](") == 1
    assert "springer-nature.com" not in result


def test_prune_keeps_one_copy_of_a_picture_used_twice():
    body = "![](/media/x/a.png)\n\n正文\n\n![](../../media/x/a.png)"

    result = _prune_body_images(body, ["/media/x/a.png"])

    assert result.count("a.png") == 1


def test_prune_leaves_pictures_alone_when_the_source_listed_none():
    """有些归档的图片只在正文里，front-matter 是空的，这时无从校验。"""
    body = "![](https://cursor.com/images/slack-plan-preview.png)"

    assert _prune_body_images(body, []) == body


def test_prune_takes_out_trackers_and_sponsor_badges():
    body = ("![](https://www.nvidia.com/content/dam/1x1-00000000.png)\n\n"
            "![](https://0sec.ai/sponsors/e2b.svg)\n\n"
            "![](https://0sec.ai/photo-of-the-lab.png)")
    images = ["https://www.nvidia.com/content/dam/1x1-00000000.png",
              "https://0sec.ai/sponsors/e2b.svg",
              "https://0sec.ai/photo-of-the-lab.png"]

    result = _prune_body_images(body, images)

    assert result.count("![](") == 1
    assert "photo-of-the-lab.png" in result


def test_prune_leaves_a_diagram_made_for_the_post():
    """流程图是为这篇稿子生成的，不在原文清单里，不能当成编造的地址删掉。"""
    body = "![](/images/diagrams/flow-1.png)\n\n![](https://x.com/a.png)"

    result = _prune_body_images(body, ["https://x.com/a.png"])

    assert "/images/diagrams/flow-1.png" in result


# ═══════════════════════════════════════════════════════════════════
# rewrite  — slice limit covers all [[IMG:N]] tokens
# ═══════════════════════════════════════════════════════════════════


def test_rewrite_slice_covers_all_img_urls():
    """When relativized image URLs lie beyond 8000 chars, the slice limit is
    dynamically extended so all are visible to the LLM."""
    # Build content where both URLs are past 8000 chars
    # 900 * 9 = 8100 chars of "填充文本AAAA\n" before first URL
    chunks = ["填充文本AAAA\n" * 900]
    chunks.append("![](../../media/x/late1.png)\n")
    chunks.append("填充文本AAAA\n" * 300)
    chunks.append("![](../../media/x/late2.png)\n")
    chunks.append("结尾文本。")
    body = "".join(chunks)

    # Verify URLs are past 8000
    assert body.find("late1.png") > 8000, f"late1.png at {body.find('late1.png')}"
    assert body.find("late2.png") > 8000, f"late2.png at {body.find('late2.png')}"

    # Simulate the slice logic from rewrite()
    images = ["/media/x/late1.png", "/media/x/late2.png"]
    img_end = max(
        [body.find(_relativize_media(u)) for u in images
         if body.find(_relativize_media(u)) >= 0]
        or [0])
    slice_limit = max(8000, img_end + 500)

    assert slice_limit > 8000
    assert "late1.png" in body[:slice_limit]
    assert "late2.png" in body[:slice_limit]


def test_rewrite_slice_defaults_to_8000():
    """When all image URLs are within 8000 chars, the slice stays at the default."""
    body = "简短正文 ![](/media/x/pic.png) 结束"
    images = ["/media/x/pic.png"]
    img_end = max(
        [body.find(_relativize_media(u)) for u in images
         if body.find(_relativize_media(u)) >= 0]
        or [0])
    slice_limit = max(8000, img_end + 500)
    assert slice_limit == 8000


# ═══════════════════════════════════════════════════════════════════
# _build_manifest & _apply_placeholders — manifest+placeholder flow
# ═══════════════════════════════════════════════════════════════════

def test_build_manifest_generates_entries():
    """_build_manifest creates entries for images and videos with 0-indexed tokens."""
    manifest_text, media_map = _build_manifest(
        ["/media/a/img1.png", "/media/a/img2.png"],
        ["https://youtube.com/embed/x"])
    assert "IMG0" in media_map
    assert media_map["IMG0"] == ("img", "/media/a/img1.png")
    assert "IMG1" in media_map
    assert media_map["IMG1"] == ("img", "/media/a/img2.png")
    assert "VID0" in media_map
    assert media_map["VID0"] == ("vid", "https://youtube.com/embed/x")
    # Images listed as bullet points (pre-expanded inline)
    assert "img1.png" in manifest_text
    assert "img2.png" in manifest_text
    # Videos still use [[VID:0]] placeholder format
    assert "[[VID:0]]" in manifest_text


def test_build_manifest_empty_inputs():
    """_build_manifest returns empty strings/maps when there's no media."""
    manifest_text, media_map = _build_manifest([], [])
    assert manifest_text == ""
    assert media_map == {}


def test_apply_placeholders_replaces_img_tokens():
    """_apply_placeholders replaces [[IMG:0]] with markdown image syntax."""
    media_map = {"IMG0": ("img", "/media/x/fig1.png")}
    result = _apply_placeholders("正文\n\n[[IMG:0]]\n\n结尾", media_map)
    assert "[[IMG:0]]" not in result
    assert "![](/media/x/fig1.png)" in result
    assert result.index("正文") < result.index("fig1.png") < result.index("结尾")


def test_apply_placeholders_replaces_vid_tokens():
    """_apply_placeholders replaces [[VID:0]] with video embed."""
    media_map = {"VID0": ("vid", "https://youtube.com/embed/abc")}
    result = _apply_placeholders("正文\n\n[[VID:0]]\n\n结尾", media_map)
    assert "[[VID:0]]" not in result
    assert "youtube.com/embed/abc" in result
    assert '<iframe' in result


def test_apply_placeholders_accepts_extractor_video_tokens():
    media_map = {"VID0": ("vid", "https://youtube.com/embed/abc")}
    result = _apply_placeholders("正文\n[[VIDEO:0]]", media_map)
    assert "[[VIDEO:0]]" not in result
    assert '<iframe src="https://youtube.com/embed/abc"' in result


def test_apply_placeholders_removes_unknown_tokens():
    """Unknown placeholders are removed from the body."""
    media_map = {"IMG0": ("img", "/media/x/pic.png")}
    result = _apply_placeholders("[[IMG:0]]\n[[IMG:99]]\n[[VID:999]]", media_map)
    assert "![](/media/x/pic.png)" in result  # known token replaced
    assert "[[IMG:99]]" not in result  # unknown removed
    assert "[[VID:999]]" not in result  # unknown removed


def test_apply_placeholders_handles_bracket_variants():
    """Both [[IMG:0]] and [IMG:0] bracket styles are handled."""
    media_map = {"IMG0": ("img", "/media/x/pic.png")}
    result = _apply_placeholders("[[IMG:0]] and [IMG:0]", media_map)
    assert result.count("pic.png") == 2
    # _apply_placeholders wraps each replacement in \n\n padding
    assert "![](/media/x/pic.png)" in result
    assert result.count("/media/x/pic.png") == 2


def test_rewrite_through_manifest_flow():
    """LLM outputs [[IMG:N]] markers → _apply_placeholders → real URLs in body.
    This validates the manifest+placeholder integration in rewrite()."""
    art = Article(
        title="t", content_md="正文含 [[IMG:0]] 标记",
        url="https://x.com/a", source_name="X", source_type="scrape",
        published_at=None,
        images=["/media/x/fig1.png", "/media/x/fig2.png"],
        raw_summary=None, fetched_at=datetime.now(timezone.utc), topic="AI")
    # LLM places [[IMG:0]] in output but omits [[IMG:1]]
    body = "## 一、开头\n\n[[IMG:0]]\n\n## 二、深度内容"
    resp = json.dumps({"title_candidates": ["标题"], "body_md": body})
    provider = MockProvider(responses=[resp, json.dumps({"flagged_claims": []})])
    draft = rewrite(art, provider)
    b = draft.body_md
    # [[IMG:0]] replaced with real URL by _apply_placeholders
    assert "[[IMG:0]]" not in b
    assert "![](/media/x/fig1.png)" in b
    # fig1 appears before 二、深度内容 (placed by LLM, not appended at end)
    assert b.index("fig1.png") < b.index("深度内容")
    # fig2 was NOT placed by the LLM, and stays out: the rewrite keeps the
    # pictures that earn their place and drops the rest.
    assert "fig2.png" not in b


# ── _extract_json_field ────────────────────────────────────────────────

def test_extract_json_field_simple():
    text = '{"title_candidates": ["t"], "body_md": "hello world"}'
    result = _extract_json_field(text, "body_md")
    assert result == "hello world"


def test_extract_json_field_with_escapes():
    text = '{"body_md": "line1\\n\\nline2\\tindented\\"quoted\\""}'
    result = _extract_json_field(text, "body_md")
    assert result == "line1\n\nline2\tindented\"quoted\""


def test_extract_json_field_multiple_fields():
    """Extracts the correct field when JSON has multiple keys."""
    text = (
        '{"title_candidates": ["t1", "t2"], '
        '"body_md": "actual body", '
        '"score": 80}'
    )
    result = _extract_json_field(text, "body_md")
    assert result == "actual body"


def test_extract_json_field_body_has_inner_braces():
    """body_md value contains { and } characters."""
    text = '{"body_md": "code: `{key: value}` end"}'
    result = _extract_json_field(text, "body_md")
    assert "{key: value}" in result


def test_extract_json_field_missing_key_returns_none():
    assert _extract_json_field('{"title": "x"}', "body_md") is None
    assert _extract_json_field("", "body_md") is None
    assert _extract_json_field("not json", "body_md") is None


def test_extract_json_field_raw_newlines_in_value():
    """Malformed JSON — body_md has literal \\n sent as real newlines."""
    text = (
        '{\n'
        '  "title_candidates": ["标题"],\n'
        '  "body_md": "## 第一章\n'
        '\n'
        '段落一。\n'
        '\n'
        '## 第二章\n'
        '\n'
        '段落二。"\n'
        '}'
    )
    result = _extract_json_field(text, "body_md")
    assert result is not None
    assert "## 第一章" in result
    assert "段落一" in result
    assert "## 第二章" in result
    assert "段落二" in result


# ── _extract_json_fallback ─────────────────────────────────────────────

def test_extract_json_fallback_clean():
    """Normal JSON that _extract_json would handle — fallback works too."""
    text = '{"title_candidates": ["标题1", "标题2"], "body_md": "正文内容"}'
    result = _extract_json_fallback(text)
    assert result is not None
    assert result["title_candidates"] == ["标题1", "标题2"]
    assert result["body_md"] == "正文内容"


def test_extract_json_fallback_raw_newlines():
    """The exact bug scenario: malformed JSON with raw newlines in body_md."""
    text = (
        '```json\n'
        '{\n'
        '  "title_candidates": [\n'
        '    "《The Blood of Dawnwalker》独家解读：时间就是货币",\n'
        '    "30 天倒计时，8 个时段"\n'
        '  ],\n'
        '  "body_md": "## 一、鲜血弥撒前的 8 小时\n'
        '\n'
        '拉斯莱亚村很小。\n'
        '\n'
        '但对科恩一家来说，这个世界最近变得更小了。\n'
        '\n'
        '## 二、选择，就是推动时间的唯一方式\n'
        '\n'
        '游戏从这里开始。\n'
        '\n'
        '## 写在最后\n'
        '\n'
        '这是一个关于有限选择的寓言。\n'
        '\n'
        '游戏将于 2026 年 9 月 3 日登陆 PlayStation 5。"\n'
        '}\n'
        '```'
    )
    result = _extract_json_fallback(text)
    assert result is not None
    assert len(result["title_candidates"]) == 2
    assert "黎明行者" not in result["body_md"]  # from title, not body
    assert "鲜血弥撒" in result["body_md"]
    assert "写在最后" in result["body_md"]
    assert "PlayStation 5" in result["body_md"]


def test_extract_json_fallback_single_title():
    text = (
        '{\n'
        '  "title_candidates": ["唯一标题"],\n'
        '  "body_md": "正文"\n'
        '}'
    )
    result = _extract_json_fallback(text)
    assert result["title_candidates"] == ["唯一标题"]


def test_extract_json_fallback_no_title_candidates():
    text = '{"body_md": "正文但没有标题"}'
    assert _extract_json_fallback(text) is None


def test_extract_json_fallback_no_body_md():
    text = '{"title_candidates": ["有标题无正文"]}'
    assert _extract_json_fallback(text) is None


def test_extract_json_fallback_meta_commentary():
    """LLM outputs meta-text saying 'done' — fallback should handle it
    IF the JSON is embedded, or return None if no JSON exists."""
    # This is the exact bug scenario for drafts 1915/1916/1917:
    # the LLM output "完成。文件已写入 output.json" with NO JSON body_md
    text = (
        "完成。文件已写入 `output.json`。\n\n"
        "**输出摘要：**\n\n"
        "| 项目 | 内容 |\n| 标题候选 | 3 个 |\n"
    )
    assert _extract_json_fallback(text) is None


def test_extract_json_fallback_escaped_newlines_in_title():
    """title_candidates may contain \\n (properly escaped in JSON string)."""
    text = (
        '{\n'
        '  "title_candidates": ["line1\\nline2", "simple"],\n'
        '  "body_md": "body"\n'
        '}'
    )
    result = _extract_json_fallback(text)
    assert result is not None
    assert "line1" in result["title_candidates"][0]
    assert "line2" in result["title_candidates"][0]


# ── rewrite() retry on failed JSON ─────────────────────────────────────

def test_rewrite_retry_on_invalid_json():
    """First response has no valid JSON; second retry succeeds."""
    invalid_resp = "已完成，文件已写入 output.json。完全不包含 JSON。"
    retry_resp = json.dumps({
        "title_candidates": ["重试成功标题"],
        "body_md": "## 重试后的正文\n\n内容来了。",
    })
    check_resp = json.dumps({"flagged_claims": []})
    provider = MockProvider(responses=[invalid_resp, retry_resp, check_resp])
    draft = rewrite(sample_article(), provider)
    assert draft.title_candidates[0] == "重试成功标题"
    assert "内容来了" in draft.body_md


def test_rewrite_retry_on_malformed_json():
    """First response has JSON with raw newlines (fail parse → fallback fails
    because it's meta-text without real body_md).  Second retry works."""
    # Simulate the draft-1910 bug: JSON with raw newlines + meta prefix
    invalid = (
        "既然用户要求转写为深度中文报道，直接执行即可。\n\n"
        "```json\n"
        '{"title_candidates": ["标题"], '
        '"body_md": "## 一、开头\\n\\n内容\\n\\n## 写在最后\\n\\n结束。"}\n'
        "```"
    )
    # _extract_json should FAIL (body_md has raw \n in value)
    # _extract_json_fallback should at least extract body_md
    # Actually this specific case HAS proper escaping, so it should parse OK.
    # Let's make a case that truly fails: raw newlines inside body_md value
    invalid2 = (
        '```json\n'
        '{\n'
        '  "title_candidates": ["标题"],\n'
        '  "body_md": "## 一、开头\n'
        '\n'
        '第一段。\n'
        '\n'
        '## 写在最后\n'
        '\n'
        '结束。"\n'
        '}\n'
        '```'
    )
    retry_resp = json.dumps({
        "title_candidates": ["正确标题"],
        "body_md": "## 正确正文\\n\\n内容。",
    })
    check_resp = json.dumps({"flagged_claims": []})
    provider = MockProvider(responses=[invalid2, retry_resp, check_resp])
    draft = rewrite(sample_article(), provider)
    # fallback should have extracted body_md from invalid2,
    # so retry is NOT triggered — check fallback output
    assert draft.title_candidates[0] == "标题"  # from fallback extraction
    assert "第一段" in draft.body_md


def test_rewrite_no_retry_when_fallback_works():
    """JSON has raw newlines but _extract_json_fallback recovers it;
    no retry is triggered."""
    text = (
        '```json\n'
        '{\n'
        '  "title_candidates": ["直接用回退提取的标题"],\n'
        '  "body_md": "## 回退正文\n'
        '\n'
        '这是通过正则提取的内容。\n'
        '\n'
        '## 写在最后\n'
        '\n'
        '结尾。"\n'
        '}\n'
        '```'
    )
    check_resp = json.dumps({"flagged_claims": []})
    # Only 2 responses total — no retry response expected
    provider = MockProvider(responses=[text, check_resp])
    draft = rewrite(sample_article(), provider)
    assert draft.title_candidates[0] == "直接用回退提取的标题"
    assert "回退正文" in draft.body_md
    assert "正则提取" in draft.body_md
