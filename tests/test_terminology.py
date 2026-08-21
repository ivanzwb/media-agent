"""术语规则得同时说清两个方向，而且只有一处说。

这条规则栽过三次：一概译成中文，AI agent 成了「AI 代理」；改成「一律保留英文」
之后，training、latency、open source 这些早有通行中文说法的词也留在句子里没译；
改成按词判断之后，短语又漏了——prompt 在留英文的名单里，plain-language prompt
就整个短语留在了中文句子中间。

三次都是只说了一个方向，而当时四处提示词各写一遍，改一处也拦不住别处跑偏。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.pipeline.rewriter import REWRITE_INSTRUCTION, REWRITE_SYSTEM
from app.pipeline.styles import build_learned_instruction, builtin_styles
from app.pipeline.synthesizer import DEPTHS, _depth_instruction
from app.pipeline.terminology import (
    MUST_TRANSLATE, PLAIN_WORDS, TERM_RULE, TERM_RULE_LINE)

APP = Path(__file__).resolve().parent.parent / "app"


def test_the_rule_says_when_to_keep_english_and_when_to_translate():
    assert "留英文" in TERM_RULE and "用中文" in TERM_RULE
    # 判断标准要写出来，不然模型只能照抄示例里那几个词。
    assert "中文技术圈平时写这个词" in TERM_RULE
    for keep in ("GPT-4", "LLM", "agent", "prompt"):
        assert keep in TERM_RULE
    # 第二次栽的那批词。
    for translate in ("训练", "推理", "微调", "延迟", "开源", "基准测试"):
        assert translate in TERM_RULE
    assert "普通词汇不是术语" in TERM_RULE
    assert "AI 代理" in TERM_RULE
    # 留英文不等于不用解释——这两件事被混过一次。
    assert "跟留不留英文是两件事" in TERM_RULE


def test_the_rule_says_english_is_only_for_the_term_itself():
    """第三次栽的地方：术语在名单里，模型就把整个短语都留成英文。"""
    assert "英文只留术语本身那一个词" in TERM_RULE
    assert "plain-language prompt" in TERM_RULE and "大白话 prompt" in TERM_RULE
    # 单句版也得带上这条，它是系统提示词里唯一的术语说明。
    assert "plain-language prompt" in TERM_RULE_LINE


def test_the_rule_says_translate_when_in_doubt():
    """没有默认动作的话，模型会挑省事的那条路——原样不动。"""
    assert "拿不准就译" in TERM_RULE and "拿不准就译" in TERM_RULE_LINE


def test_the_examples_show_both_directions():
    """示例只给「该译的」，模型会矫正到另一边去，把产品名也译了。"""
    assert "| GPT-4 Turbo | GPT-4 Turbo |" in TERM_RULE
    assert "| inference latency | 推理延迟 |" in TERM_RULE


def test_the_word_lists_reach_the_rule():
    for english, chinese in MUST_TRANSLATE + PLAIN_WORDS:
        assert f"{english} 写「{chinese}」" in TERM_RULE


def test_the_rule_says_the_word_list_is_only_a_sample():
    """哪些词该译是个开放集合，不说清是举例，模型会读成「只有这几个要译」。"""
    assert "这几个只是举例" in TERM_RULE
    assert "不在这份例子里不等于可以留着英文" in TERM_RULE


def test_the_short_form_still_carries_both_directions():
    """系统提示词和表格塞不下整段，塞进去的那一句不能只说一半。"""
    assert "留英文原文" in TERM_RULE_LINE
    assert "译成中文" in TERM_RULE_LINE
    assert "普通词汇" in TERM_RULE_LINE


def test_the_default_rewrite_carries_the_rule():
    assert TERM_RULE in REWRITE_INSTRUCTION
    assert TERM_RULE_LINE in REWRITE_SYSTEM


@pytest.mark.parametrize("style", builtin_styles(), ids=lambda s: s.id)
def test_every_builtin_style_carries_the_rule(style):
    assert TERM_RULE in style.instruction


def test_a_learned_style_carries_the_rule():
    """学出来的风格换掉的是语气，术语规则不跟着换。"""
    assert TERM_RULE in build_learned_instruction("写得像老友聊天")


@pytest.mark.parametrize("depth", sorted(DEPTHS))
def test_synthesis_carries_the_rule_at_every_depth(depth):
    assert TERM_RULE_LINE in _depth_instruction(depth)


def test_the_wording_lives_in_one_place_only():
    """四处提示词各写一遍时，改一处拦不住别处跑偏——所以只准这一处写。"""
    elsewhere = [
        path.relative_to(APP).as_posix()
        for path in APP.rglob("*.py")
        if path.name != "terminology.py"
        and "保留英文原文" in path.read_text(encoding="utf-8")
    ]
    assert elsewhere == [], (
        "术语规则的措辞只应写在 app/pipeline/terminology.py 里，"
        f"改这些文件时请引用 TERM_RULE/TERM_RULE_LINE：{elsewhere}")
