"""术语规则得同时说清两个方向，而且写出来的稿子要有人查。

这条规则栽过三次：一概译成中文，AI agent 成了「AI 代理」；改成「一律保留英文」
之后，training、latency、open source 这些早有通行中文说法的词也留在句子里没译；
改成按词判断之后，短语又漏了——prompt 在留英文的名单里，plain-language prompt
就整个短语留在了中文句子中间。

前两次都是靠提示词硬扛、坏了没人发现。所以这里既钉规则的形状，也钉
check_terms 的判断：哪些该报、哪些不许报。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models import Draft
from app.pipeline.rewriter import REWRITE_INSTRUCTION, REWRITE_SYSTEM
from app.pipeline.score import grade_draft
from app.pipeline.styles import build_learned_instruction, builtin_styles
from app.pipeline.synthesizer import DEPTHS, _depth_instruction
from app.pipeline.terminology import (
    KEEP_WORDS, KEPT_PHRASES, MUST_TRANSLATE, PLAIN_WORDS, TERM_RULE,
    TERM_RULE_LINE, check_terms, term_samples)

APP = Path(__file__).resolve().parent.parent / "app"


def kinds(findings):
    return [f["kind"] for f in findings]


def texts(findings):
    return [f["text"] for f in findings]


# ── 规则的形状 ──────────────────────────────────────────────────────────

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


def test_the_word_list_and_the_rule_cannot_drift():
    """词表给出的中文说法，提示词里必须逐条写着，反之亦然。"""
    for english, chinese in MUST_TRANSLATE + PLAIN_WORDS:
        assert f"{english} 写「{chinese}」" in TERM_RULE


def test_the_rule_says_the_word_list_is_only_a_sample():
    """不说清是举例，模型会把这几个词当成「只有这些要译」。"""
    assert "这几个只是举例" in TERM_RULE
    assert "不在这份例子里不等于可以留着英文" in TERM_RULE


def test_the_short_form_still_carries_both_directions():
    assert "留英文原文" in TERM_RULE_LINE
    assert "译成中文" in TERM_RULE_LINE
    assert "普通词汇" in TERM_RULE_LINE


# ── 规则得送到每条会翻译原文的路上 ──────────────────────────────────────

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


# ── 检查成稿：该报的 ────────────────────────────────────────────────────

def test_the_phrase_that_started_all_this_is_caught():
    found = check_terms("这套做法的关键是 plain-language prompt，作者这么叫它。")
    assert kinds(found) == ["phrase"]
    assert found[0]["text"] == "plain-language prompt"
    assert "修饰它的词" in found[0]["hint"]


def test_a_word_with_a_settled_chinese_spelling_is_caught():
    found = check_terms("这一轮 training 用了 8 张卡，performance 提升明显。")
    assert kinds(found) == ["word", "word"]
    assert texts(found) == ["training", "performance"]
    # 词表里有的，顺手把中文说法一起报出来。
    assert [f["hint"] for f in found] == ["写「训练」", "写「性能」"]


def test_a_word_no_list_could_have_predicted_is_caught():
    """换个主题就换一批词，词表永远列不全——所以判断不能靠词表。"""
    for word in ("evaluation", "scaffolding", "checkpoint", "guardrails",
                 "provenance", "watermarking"):
        found = check_terms(f"这次 {word} 是自己做的。")
        assert texts(found) == [word], word
        assert found[0]["hint"] == "不是名字也不是缩写，中文技术圈有说法就译过来"


def test_plurals_count_as_the_same_word():
    assert texts(check_terms("两个 datasets 的 weights 都换了。")) == [
        "datasets", "weights"]


def test_a_whole_english_sentence_is_caught():
    found = check_terms("他补了一句 the model is not thinking，然后就走了。")
    assert kinds(found) == ["phrase"]
    assert found[0]["text"] == "the model is not thinking"


def test_findings_come_back_in_line_order():
    body = "第一行讲 inference latency。\n第二行没有英文。\n第三行讲 training。"
    assert [f["line"] for f in check_terms(body)] == [1, 3]


def test_one_spot_is_reported_once():
    """inference 和 latency 各自也在词表里，但这里只该报一条短语。"""
    found = check_terms("把 inference latency 压到 200 毫秒。")
    assert kinds(found) == ["phrase"]


# ── 检查成稿：不许报的 ──────────────────────────────────────────────────

def test_a_term_standing_on_its_own_is_left_alone():
    """留英文本来就是对的，报了等于逼人把 agent 译成「代理」。"""
    for line in ("给 agent 写 prompt 的时候要注意 token 数量。",
                 "LLM 和 RAG 都是这么回事，MoE 也一样。",
                 "Transformer 之后大家都用 GPU 训练。"):
        assert check_terms(line) == [], line


def test_a_single_name_or_acronym_needs_no_list():
    """大写就认得出是名字和缩写，不用一个个列出来。"""
    for line in ("这次用 Claude 跑的，比 Qwen 稳。",
                 "SDK 和 CUDA 的版本都得对上。",
                 "Llama 之后 Mistral 也开源了。"):
        assert check_terms(line) == [], line


def test_a_gloss_in_brackets_is_what_the_rule_asked_for():
    """「上下文窗口（context window）」是规则自己建议的写法，不能反手报它。"""
    assert check_terms("先看上下文窗口（context window）这个数。") == []
    # 括号里夹着中文的照查，别拿括号当挡箭牌。
    assert texts(check_terms("先看窗口（这一步叫 sliding window）。")) == [
        "sliding window"]


def test_units_stuck_to_numbers_are_not_words():
    assert check_terms("128k 上下文，延迟 200ms，显存 8gb。") == []


def test_names_are_not_untranslated_english():
    for name in ("GPT-4 Turbo", "Hugging Face", "Stable Diffusion",
                 "Mixture of Experts", "Attention Is All You Need"):
        assert check_terms(f"这事得看 {name} 怎么做。") == [], name


def test_the_few_phrases_chinese_writing_keeps_are_left_alone():
    for phrase in sorted(KEPT_PHRASES):
        assert check_terms(f"先看 {phrase} 那一段。") == [], phrase


def test_the_keep_list_is_short_enough_to_be_honest():
    """该留英文的小写词是个封闭的小集合——它一长，就说明又在拿词表当闸门了。"""
    assert len(KEEP_WORDS) < 20
    for word in KEEP_WORDS:
        assert word.islower(), word
        assert check_terms(f"这里说的是 {word}。") == [], word


def test_code_and_links_are_not_prose():
    body = (
        "```bash\nnpm run build --watch\n```\n"
        "把 `--max-tokens` 调小一点。\n"
        "详见 https://example.com/why-agents-keep-failing 这一篇。\n"
        "![a plain language prompt](https://example.com/shot.png)\n"
        ":::tip some english label\n这一段是中文，卡片里的正文照样要查。\n:::\n"
        "<span class=\"note\">看这里</span>\n")
    assert check_terms(body) == []


def test_a_card_body_is_still_the_article():
    """组件的参数写在 ::: 那一行上，卡片里包着的是读者要读的正文。"""
    body = ":::tip 划重点\n关键是 plain-language prompt 这一步。\n:::\n"
    assert texts(check_terms(body)) == ["plain-language prompt"]


def test_quotes_and_source_lines_may_stay_english():
    """引文和出处按规矩就该留英文原文，报出来是给人添不能改的账。"""
    body = (
        "> the model is not thinking, it is matching patterns\n"
        "原文：Why Your Agent Keeps Failing At Long Tasks\n"
        "来源：Some Tech Blog\n")
    assert check_terms(body) == []


def test_the_reference_list_is_not_the_article():
    body = ("正文就这一句。\n\n"
            "## 参考文献\n\n"
            "1. Why your agent keeps failing —— Example Blog\n")
    assert check_terms(body) == []


def test_nothing_to_check_is_not_a_finding():
    for body in ("", "   ", "整篇都是中文，一个英文单词都没有。"):
        assert check_terms(body) == []


# ── 报给人看的形状 ──────────────────────────────────────────────────────

def test_only_the_first_few_hits_go_into_the_progress_line():
    found = check_terms(
        "先 training，再 inference，然后 deployment，最后 accuracy。")
    assert len(found) == 4
    assert term_samples(found) == "training、inference、deployment…"


def test_the_samples_do_not_repeat_the_same_word():
    found = check_terms("training 之后又 training，然后才 accuracy。")
    assert term_samples(found) == "training、accuracy"


def test_a_graded_draft_reports_what_was_left_in_english():
    """两轮提示词都是坏了没人发现——评分必须把这事说出来。"""
    draft = Draft(
        article_id=1,
        title_candidates=["写给工程师的 agent 入门"],
        body_md="这套做法的关键是 plain-language prompt，training 也换了。",
        topic="agent",
        source_url="https://example.com/a",
        source_name="Example",
    )
    grade = grade_draft(draft)
    assert texts(grade.terms) == ["plain-language prompt", "training"]
