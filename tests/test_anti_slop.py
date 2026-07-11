"""Tests for the anti-AI-slop system (app.pipeline.anti_slop)."""

import json
from datetime import datetime, timezone

from app.models import Article
from app.llm.providers.mock import MockProvider
from app.pipeline.anti_slop import (
    ANTI_SLOP_SYSTEM_INSTRUCTION,
    post_process,
    _SLOP_PATTERNS,
)
from app.pipeline.rewriter import rewrite, REWRITE_SYSTEM
from app.pipeline import styles as S


# ── ANTI_SLOP_SYSTEM_INSTRUCTION ─────────────────────────────────────────


def test_instruction_is_non_empty():
    assert ANTI_SLOP_SYSTEM_INSTRUCTION.strip()


def test_instruction_contains_key_rules():
    text = ANTI_SLOP_SYSTEM_INSTRUCTION
    assert "空泛开头" in text
    assert "假深刻" in text
    assert "报告套话" in text
    assert "科技博客体" in text
    assert "公众号体" in text
    assert "假口语" in text
    assert "机械结构" in text
    assert "结尾升华" in text
    assert "堆砌修辞" in text


def test_instruction_contains_key_forbidden_words():
    text = ANTI_SLOP_SYSTEM_INSTRUCTION
    for word in ["赋能", "抓手", "沉淀", "深入浅出", "干货满满", "懂的都懂"]:
        assert word in text


# ── _SLOP_PATTERNS coverage ──────────────────────────────────────────────


def test_all_patterns_have_removal_replacement():
    """Every slop pattern should use empty-string replacement (remove)."""
    for pattern, replacement in _SLOP_PATTERNS:
        assert replacement == "", f"Pattern {pattern.pattern} should remove, not replace"


# ── post_process ─────────────────────────────────────────────────────────


class TestPostProcessOpeningSlop:
    def test_removes_suizhe_opening(self):
        text = "随着人工智能技术的快速发展，我们看到了重大突破。"
        result = post_process(text)
        assert "随着人工智能技术的快速发展" not in result
        assert "重大突破" in result or result.strip() == ""

    def test_removes_jinnianlai(self):
        text = "近年来，AI 技术在各个领域得到了广泛应用。\n正文继续。"
        result = post_process(text)
        assert "近年来" not in result
        assert "正文继续" in result

    def test_removes_zai_dangjin(self):
        text = "在当今数字化时代，企业需要转型。\n新段落。"
        result = post_process(text)
        assert "在当今" not in result
        assert "新段落" in result

    def test_removes_langchao_opening(self):
        text = "在 AI 技术的浪潮中，创新不断涌现。"
        result = post_process(text)
        assert "浪潮" not in result

    def test_does_not_damage_normal_text(self):
        text = "OpenAI 今天发布了 GPT-5。这是他们至今最强的模型。"
        result = post_process(text)
        assert "OpenAI 今天发布了 GPT-5" in result


class TestPostProcessFakeDepth:
    def test_removes_bujinshi_gengshi(self):
        text = "这不仅是技术的进步，更是思维方式的革命。"
        result = post_process(text)
        assert "不仅是" not in result and "更是" not in result

    def test_removes_biaomianshang_benzhi(self):
        text = "表面上这是一次产品更新，本质上是一场战略转型。"
        result = post_process(text)
        assert "表面上" not in result

    def test_does_not_damage_conditional_text(self):
        text = "如果不仅是速度提升了，能效也提高了 30%。"
        result = post_process(text)
        # "不仅是" here doesn't have the fake-depth structure (no "更是")
        # Our pattern requires "更是" so this should be preserved
        assert "不仅是" in result and "能效" in result


class TestPostProcessReportJargon:
    def test_removes_zhashi(self):
        assert "扎实推进" not in post_process("要扎实推进各项工作。")

    def test_removes_xingcheng_bihuan(self):
        assert "形成闭环" not in post_process("管理系统形成闭环。")

    def test_removes_nengfu(self):
        assert "赋能" not in post_process("AI 赋能各行各业。")

    def test_removes_zhuashou(self):
        assert "抓手" not in post_process("把数据作为重要抓手。")

    def test_removes_chengyu(self):
        assert "沉淀" not in post_process("知识需要沉淀。")

    def test_does_not_damage_legitimate_text(self):
        text = "新算法将训练时间缩短了 3 倍。这个结果令人振奋。"
        result = post_process(text)
        assert "训练时间" in result


class TestPostProcessTechBlog:
    def test_removes_shenruqianchu(self):
        assert "深入浅出" not in post_process("本文深入浅出地讲解了原理。")

    def test_removes_ganhuo(self):
        assert "干货满满" not in post_process("这篇干货满满的文章。")

    def test_removes_baomu(self):
        assert "保姆级教程" not in post_process("这是一份保姆级教程。")

    def test_removes_yizhanshi(self):
        assert "一站式" not in post_process("提供一站式解决方案。")

    def test_removes_burongcuoguo(self):
        assert "不容错过" not in post_process("精彩内容不容错过。")


class TestPostProcessWeChatStyle:
    def test_removes_yuanwomen(self):
        assert "愿我们都能" not in post_process("愿我们都能在AI时代找到自己的位置。")

    def test_removes_zhecai(self):
        assert "这才是真正的" not in post_process("这才是真正的成长。")


class TestPostProcessFakeSpoken:
    def test_removes_shuoshihua(self):
        assert "说实话" not in post_process("说实话，这个结果出乎意料。")

    def test_removes_nihuifxian(self):
        assert "你会发现" not in post_process("你会发现，其实很简单。")

    def test_removes_dongdedong(self):
        assert "懂的都懂" not in post_process("懂的都懂，不用多说。")

    def test_removes_nipin(self):
        assert "你品" not in post_process("你品，你细品。")


class TestPostProcessMechanical:
    def test_removes_zongdelaishuo(self):
        assert "总的来说" not in post_process("总的来说，这个方案有三个优势。")

    def test_removes_zongshangsuoshu(self):
        assert "综上所述" not in post_process("综上所述，我们建议采纳。")


class TestPostProcessCleanup:
    def test_removes_excess_blank_lines(self):
        text = "段落一。\n\n\n\n段落二。"
        result = post_process(text)
        assert result.count("\n\n") <= 1  # at most one double-newline
        assert "\n\n\n" not in result

    def test_removes_leading_blank_lines(self):
        text = "\n\n正文开始。"
        result = post_process(text)
        assert not result.startswith("\n")

    def test_multiple_patterns_in_one_pass(self):
        text = (
            "近年来，AI 技术蓬勃发展。\n\n"
            "随着深度学习技术的突破，这不仅是算法的进步，更是思维的变革。"
            "\n\n"
            "总的来说，AI 赋能各行各业，干货满满，不容错过。"
        )
        result = post_process(text)
        assert "近年来" not in result
        assert "随着深度学习" not in result
        assert "不仅是" not in result
        assert "总的来说" not in result
        assert "赋能" not in result
        assert "干货满满" not in result
        assert "不容错过" not in result
        # Core factual content should remain
        assert "AI" in result

    def test_empty_string(self):
        assert post_process("") == ""

    def test_clean_text_unchanged(self):
        text = "GPT-5 今天发布。它的推理能力比前代提升了 3 倍。"
        assert post_process(text) == text


# ── Integration: styles include anti-slop rules ──────────────────────────


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


def _article():
    return Article(
        title="New AI Model", content_md="The model scores 90 on bench.",
        url="https://x.com/a", source_name="X Blog", source_type="rss",
        published_at=None, images=[], raw_summary=None,
        fetched_at=datetime.now(timezone.utc), topic="AI")


def test_default_style_includes_anti_slop_in_system_prompt():
    """The default deep-tech style's system prompt includes anti-slop rules."""
    assert ANTI_SLOP_SYSTEM_INSTRUCTION in REWRITE_SYSTEM


def test_default_style_instruction_includes_anti_slop():
    """The injected common rules appear in the rendered instruction."""
    resp = json.dumps({"title_candidates": ["标题"], "body_md": "正文"})
    check = json.dumps({"flagged_claims": []})
    provider = RecordingProvider([resp, check])
    rewrite(_article(), provider)
    user_msg = provider.calls[0][1].content
    assert "空泛开头" in user_msg
    assert "假口语" in user_msg
    assert "赋能" in user_msg


def test_all_builtin_styles_include_anti_slop():
    """Every builtin style's rendered instruction contains anti-slop rules."""
    for style in S.builtin_styles():
        resp = json.dumps({"title_candidates": ["标题"], "body_md": "正文"})
        check = json.dumps({"flagged_claims": []})
        provider = RecordingProvider([resp, check])
        rewrite(_article(), provider, style=style)
        user_msg = provider.calls[0][1].content
        assert "空泛开头" in user_msg, f"Style '{style.id}' missing anti-slop"
        assert "假口语" in user_msg, f"Style '{style.id}' missing anti-slop"


# ── Integration: post_process runs during rewrite ────────────────────────


def test_rewrite_applies_post_processing():
    """post_process() is called on body_md during rewrite()."""
    # Body contains slop that post_process should clean
    body_md = (
        "近年来，深度学习技术快速发展。\n\n"
        "总的来说，AI 正在赋能各行各业。\n\n"
        "正文实质内容。"
    )
    resp = json.dumps({"title_candidates": ["标题"], "body_md": body_md})
    check = json.dumps({"flagged_claims": []})
    provider = MockProvider(responses=[resp, check])
    draft = rewrite(_article(), provider)
    # Slop patterns removed
    assert "近年来" not in draft.body_md
    assert "总的来说" not in draft.body_md
    assert "赋能" not in draft.body_md
    # Core content preserved
    assert "正文实质内容" in draft.body_md
