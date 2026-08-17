"""选题框里的东西，得先读懂再去搜。

用户填进来的可能是一个词、一句要求、一个问题，也可能是一段网址。以前这些原样
进搜索引擎（只去掉标点和疑问尾巴），于是搜的是那句话本身；粘网址时搜的就是
网址。这里盯住的是：理解在前、检索词在后，且理解不了也不能把整轮跑挂掉。
"""

from __future__ import annotations

from app.llm.providers.mock import MockProvider
from app.pipeline.topic_intent import (
    TopicIntent, topic_search_terms, understand_topic)


class RecordingProvider(MockProvider):
    def __init__(self, responses=None):
        super().__init__(responses)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    def chat(self, messages, **opts):
        self.prompts.append(messages[-1].content if messages else "")
        self.systems.append("\n".join(
            m.content for m in messages if m.role == "system"))
        return super().chat(messages, **opts)


def test_the_understanding_comes_back_with_the_queries():
    provider = RecordingProvider([
        '{"subject":"远程办公对团队产出的影响",'
        '"goal":"想判断要不要长期推行远程办公",'
        '"audience":"技术团队管理者",'
        '"focus":["长期影响","团队协作"],'
        '"queries":["远程办公 生产率 研究","remote work productivity study"]}'
    ])

    intent = understand_topic("长期远程办公，真的好吗", "zh", provider)

    assert intent.understood
    assert intent.subject == "远程办公对团队产出的影响"
    assert intent.goal == "想判断要不要长期推行远程办公"
    assert intent.audience == "技术团队管理者"
    assert intent.focus == ("长期影响", "团队协作")
    # 主题打头：它是这个选题最适合拿去搜的形式，后面的排序也按它评分。
    assert intent.queries == (
        "远程办公对团队产出的影响", "远程办公 生产率 研究",
        "remote work productivity study")
    # 原话留着，后面的提示词要靠它保住用户自己的说法。
    assert intent.raw == "长期远程办公，真的好吗"


def test_the_prompt_asks_for_the_reading_before_the_keywords():
    provider = RecordingProvider(['{"subject":"A","queries":["a"]}'])
    understand_topic("远程办公", "zh", provider)
    prompt = provider.prompts[0]

    # 先理解、再出词，两步的顺序写在提示词里，字段顺序也照这个来。
    assert prompt.index("第一步") < prompt.index("第二步")
    assert prompt.index('"subject"') < prompt.index('"queries"')
    for field in ("subject", "goal", "audience", "focus"):
        assert f"- {field}：" in prompt
    # 输入可能根本不是主题，这一点要明说。
    assert "网址" in prompt
    assert "选题编辑" in provider.systems[0]


def test_a_pasted_url_is_understood_instead_of_searched():
    """粘一个仓库地址进来，要搜的是这个项目，不是这串网址。"""
    provider = RecordingProvider([
        '{"subject":"watermarks-remover 开源去水印工具",'
        '"goal":"想知道这个项目能不能用来批量去水印",'
        '"audience":"","focus":[],'
        '"queries":["开源 去水印 工具 对比","watermark removal open source"]}'
    ])

    intent = understand_topic(
        "https://github.com/guillaumemeyer/watermarks-remover", "zh", provider)

    assert intent.subject == "watermarks-remover 开源去水印工具"
    assert not any("github.com" in query for query in intent.queries)
    # 网址本身进了提示词，模型得看着它判断。
    assert "github.com/guillaumemeyer" in provider.prompts[0]


def test_both_languages_are_asked_for_whatever_the_article_language():
    for lang in ("zh", "en", "bilingual"):
        provider = RecordingProvider(
            ['{"subject":"远程办公",'
             '"queries":["远程办公 研究","remote work study"]}'])
        assert understand_topic("远程办公", lang, provider).queries == (
            "远程办公", "远程办公 研究", "remote work study")
        assert "中文和英文检索词都要有" in provider.prompts[0]


def test_a_model_that_only_returns_queries_still_counts_as_understood():
    """少给一个字段不至于退回正则；检索词已经是读过输入之后写的。"""
    provider = MockProvider(['{"queries":["AI research","AI news","AI news"]}'])
    intent = understand_topic("AI", "en", provider)

    assert intent.understood
    assert intent.subject == "AI"
    assert intent.queries == ("AI", "AI research", "AI news")


def test_unusable_answers_fall_back_to_local_terms():
    for answer in ("not json", "", "[0,1,2,3]", '{"queries":[]}',
                   '{"subject":"","queries":["  "]}'):
        intent = understand_topic(
            "长期远程办公，真的好吗", "zh", MockProvider([answer]))
        assert not intent.understood, answer
        assert intent.subject == "长期远程办公"
        assert list(intent.queries) == [
            "长期远程办公", "长期远程办公 分析", "长期远程办公 专家 建议",
            "长期远程办公 research", "长期远程办公 expert analysis"]


def test_a_provider_that_raises_does_not_take_the_run_down():
    class Broken:
        def chat(self, messages, **opts):
            raise RuntimeError("上游 429")

    intent = understand_topic("远程办公", "zh", Broken())
    assert not intent.understood
    assert intent.subject == "远程办公"
    assert intent.queries[0] == "远程办公"


def test_the_offline_fallback_still_carries_english_terms():
    # 兜底不会翻译，只能在原词上挂英文限定词。真正的双语检索词来自模型那条路，
    # 这里只是不让降级后的检索退成单语言。
    for lang in ("zh", "en", "bilingual"):
        queries = understand_topic("远程办公", lang, MockProvider(["nope"])).queries
        assert queries[0] == "远程办公"
        assert any("分析" in query for query in queries)
        assert any("research" in query for query in queries)


def test_a_rambling_answer_cannot_bloat_the_prompts_it_travels_into():
    provider = MockProvider([
        '{"subject":"' + "主" * 200 + '",'
        '"goal":"' + "标" * 400 + '",'
        '"audience":"' + "读" * 100 + '",'
        '"focus":["' + '","'.join("重" * 80 for _ in range(9)) + '"],'
        '"queries":["' + '","'.join(f"词{i}" for i in range(12)) + '"]}'
    ])
    intent = understand_topic("远程办公", "zh", provider)

    assert len(intent.subject) == 80
    assert len(intent.goal) == 200
    assert len(intent.audience) == 40
    assert len(intent.focus) == 5
    assert all(len(item) == 40 for item in intent.focus)
    assert len(intent.queries) == 6


def test_the_brief_is_only_written_when_a_model_actually_read_the_input():
    understood = TopicIntent(
        raw="讲清楚 RAG，读者是后端工程师", subject="RAG 检索增强生成",
        goal="想搭出一套能用的 RAG", audience="后端工程师",
        focus=("落地踩坑",), understood=True)
    brief = understood.brief()
    assert "RAG 检索增强生成" in brief
    assert "后端工程师" in brief
    assert "落地踩坑" in brief
    assert understood.summary() == "RAG 检索增强生成（想搭出一套能用的 RAG）"

    # 兜底那份里没有模型的判断，写进下游提示词只是把它已知的话重复一遍。
    assert TopicIntent.fallback("RAG 怎么落地", "zh").brief() == ""


def test_local_terms_drop_function_words_without_touching_the_subject():
    assert topic_search_terms("长期远程办公，真的好吗", "zh") == "长期远程办公"
    assert topic_search_terms("远程办公 到底 值不值得", "zh") == "远程办公 值不值得"
    # 英文不做这类切分：疑问词表是按中文写的。
    assert topic_search_terms("is remote work good?", "en") == "is remote work good"
    assert topic_search_terms("？？？", "zh") == "？？？"


def test_an_empty_topic_never_reaches_the_model():
    provider = RecordingProvider(['{"subject":"x","queries":["x"]}'])
    intent = understand_topic("   ", "zh", provider)
    assert provider.prompts == []
    assert not intent.understood
