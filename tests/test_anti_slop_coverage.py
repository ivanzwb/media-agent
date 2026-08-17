"""每个模型调用点都得就「要不要防 AI 腔」表过态。

注入不是越多越好。检索词、JSON 下标、Mermaid 节点名、分类标签这些地方，
「正常人会这样说吗」无从谈起，把一大段写作规则塞进去只是烧 token，还可能把
真正脆弱的格式约束挤散。该注入的是产出读者会读到的成段文字的地方。

下面两份清单是逐个调用点看过之后的判断。谁新加一个模型调用，这个测试就会
失败，迫使他把自己那一处归到某一类里去——这是整个项目里唯一一道能拦住
「悄悄多出一个不防 AI 腔的写作入口」的闸。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.pipeline.anti_slop import ANTI_SLOP_SYSTEM_INSTRUCTION

APP = Path(__file__).resolve().parent.parent / "app"

# 产出读者会读到的成段文字，必须注入。
WRITES_PROSE = {
    "pipeline/synthesizer.py": 2,    # 搜索/系列创作的正文
    "pipeline/rewriter.py": 4,       # 单篇改写，及其两种重试（JSON 无效、缩水）
    "pipeline/script.py": 1,         # 口播旁白，是要念出来的
    "platforms/__init__.py": 1,      # 分平台改写，发出去的就是它
    "platforms/video_prepare.py": 1, # 视频简介文案
    "web/server.py": 4,              # 套用模板 / Agent 编辑重写正文
    "pipeline/series_create.py": 3,  # 章节 scope 会进到写作提示词里
}

# 产出的是检索词、下标、标签、评分、Mermaid 节点名一类的短标记或 JSON。
# 写作规则在这里没有意义，也不该出现。
NO_PROSE = {
    "pipeline/search_create.py": 1,  # 按主题排序的下标
    "pipeline/topic_intent.py": 1,   # 读懂选题 + 检索词，产出的是短字段和 JSON
    "pipeline/relevance.py": 1,      # 相关性筛选 JSON
    "pipeline/recommender.py": 6,    # 子主题、关键词、源名、热度
    "pipeline/classifier.py": 1,     # 单个主题标签
    "pipeline/score.py": 1,          # 评分 JSON，理由不入库
}

# 例外：风格学习产出的是「给写作模型的规则」，不是正文。照搬那段写作规则没有
# 意义，它需要的是另一句话——别把套话归纳成风格。见 style_learner._SYSTEM_PROMPT。
LEARNS_STYLE = {"pipeline/style_learner.py": 1}

ALL_SITES = {**WRITES_PROSE, **NO_PROSE, **LEARNS_STYLE}


def _sources() -> dict[str, str]:
    return {
        path.relative_to(APP).as_posix(): path.read_text(encoding="utf-8")
        for path in APP.rglob("*.py")
    }


def test_every_model_call_site_is_accounted_for():
    found = {name: text.count(".chat(")
             for name, text in _sources().items() if ".chat(" in text}
    assert found == ALL_SITES, (
        "模型调用点变了。请把新增的一处归入 WRITES_PROSE（产出正文，必须注入"
        "ANTI_SLOP_SYSTEM_INSTRUCTION）或 NO_PROSE（只产出标记/JSON），"
        f"实际：{found}")


@pytest.mark.parametrize("name", sorted(WRITES_PROSE))
def test_prose_writers_inject_the_anti_slop_rules(name):
    assert "ANTI_SLOP_SYSTEM_INSTRUCTION" in (APP / name).read_text(
        encoding="utf-8")


@pytest.mark.parametrize("name", sorted(NO_PROSE))
def test_marker_only_calls_stay_out_of_it(name):
    """把写作规则塞进检索词生成器是照猫画虎，不是覆盖率。"""
    assert "ANTI_SLOP_SYSTEM_INSTRUCTION" not in (APP / name).read_text(
        encoding="utf-8")


def test_the_learned_style_refuses_the_patterns_by_name():
    from app.pipeline.style_learner import _SYSTEM_PROMPT

    for pattern in ("随着", "不仅是", "深入浅出", "说实话"):
        assert pattern in _SYSTEM_PROMPT
    assert "style_guidance" in _SYSTEM_PROMPT


def test_the_rules_still_say_what_the_tests_look_for():
    """上面几处断言认的是这些字样，改词表时别把它们改没了。"""
    for anchor in ("严禁 AI 腔", "忌破折号"):
        assert anchor in ANTI_SLOP_SYSTEM_INSTRUCTION
