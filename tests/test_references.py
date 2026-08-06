from __future__ import annotations

from app.pipeline import references
from app.pipeline.ai_flavor import check_text


BODY = "## 一、开头\n正文第一段 [1]。\n\n## 二、后续\n第二段 [2]。"
TAIL = "\n\n---\n\n## 参考文献\n1. 甲文 — 甲媒体\n2. 乙文 — 乙媒体\n"


def test_split_returns_the_two_halves_of_the_original():
    prose, tail = references.split(BODY + TAIL)
    assert prose == BODY
    assert tail == TAIL
    assert prose + tail == BODY + TAIL


def test_an_article_without_a_reference_list_is_left_alone():
    assert references.split(BODY) == (BODY, "")


def test_the_separator_travels_with_the_list():
    """It belongs to the list, not the prose — otherwise every rewrite leaves
    another stray horizontal rule behind."""
    _, tail = references.split(BODY + TAIL)
    assert tail.lstrip("\n").startswith("---")


def test_restore_puts_the_list_back_exactly_as_it_was():
    prose, tail = references.split(BODY + TAIL)
    assert references.restore(prose, tail) == BODY + TAIL


def test_restore_keeps_the_blank_line_before_the_rule():
    """Without it the rule becomes a setext underline and markdown turns the
    last line of prose into a heading."""
    _, tail = references.split(BODY + TAIL)
    assert "\n\n---" in references.restore("重写后的正文", tail)


def test_the_models_own_reference_list_loses_to_the_real_one():
    _, tail = references.split(BODY + TAIL)
    invented = "重写后的正文\n\n## 参考文献\n1. 模型编的 — 无\n"
    restored = references.restore(invented, tail)
    assert "模型编的" not in restored
    assert restored.count("## 参考文献") == 1
    assert "甲文 — 甲媒体" in restored


def test_a_last_line_of_prose_is_not_mistaken_for_the_list():
    body = "## 正文\n本文参考文献综述的写法值得一看。"
    assert references.split(body) == (body, "")


def test_the_reference_list_is_not_counted_as_ai_flavour():
    """Every entry carries an em dash the pipeline itself wrote; charging the
    article for them would hand out advice about a machine-made list."""
    prose_only = check_text(BODY)
    with_refs = check_text(BODY + TAIL)
    assert with_refs["chars"] == prose_only["chars"]
    assert not [f for f in with_refs["findings"] if f["key"] == "dash"]
