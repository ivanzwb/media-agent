"""选题框里的东西，得先读懂再去搜。

用户填进来的可能是一个词、一句要求、一个问题，也可能是一段网址。以前这些原样
进搜索引擎（只去掉标点和疑问尾巴），于是搜的是那句话本身；粘网址时搜的就是
网址。这里盯住的是：理解在前、检索词在后，且理解不了也不能把整轮跑挂掉。
"""

from __future__ import annotations

from app.llm.providers.mock import MockProvider
from app.pipeline.topic_intent import (
    TopicIntent, atomize_query, query_terms, shorten_query, topic_search_terms,
    understand_topic)


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
    # subject 是写给人读的一句话，恰恰是搜索引擎匹配不了的东西，不进检索词。
    assert intent.queries == (
        "远程办公 生产率 研究", "remote work productivity study")
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
    assert intent.queries == ("开源 去水印 工具", "watermark removal open source")
    assert not any("github.com" in query for query in intent.queries)
    # 网址本身进了提示词，模型得看着它判断。
    assert "github.com/guillaumemeyer" in provider.prompts[0]


def test_both_languages_are_asked_for_whatever_the_article_language():
    for lang in ("zh", "en", "bilingual"):
        provider = RecordingProvider(
            ['{"subject":"远程办公",'
             '"queries":["远程办公 研究","remote work study"]}'])
        assert understand_topic("远程办公", lang, provider).queries == (
            "远程办公 研究", "remote work study")
        assert "中文和英文检索词都要有" in provider.prompts[0]


def test_a_model_that_only_returns_queries_still_counts_as_understood():
    """少给一个字段不至于退回正则；检索词已经是读过输入之后写的。"""
    provider = MockProvider(['{"queries":["AI research","AI news","AI news"]}'])
    intent = understand_topic("AI", "en", provider)

    assert intent.understood
    assert intent.subject == "AI"
    assert intent.queries == ("AI research", "AI news")


def test_stacked_queries_are_cut_down_before_they_are_searched():
    """一条词堆三四个概念，引擎全 AND 起来谁也满足不了，返回零结果。"""
    provider = MockProvider([
        '{"subject":"大模型输出水印的识别与去除",'
        '"queries":["大模型输出水印的识别与去除技术",'
        '"LLM watermark detection removal techniques",'
        '"Claude output watermark"]}'
    ])
    intent = understand_topic("怎么去掉大模型输出的水印", "zh", provider)

    # 汉字数超了就从尾巴上砍：留下的是它开头那个最有区分度的概念。「去除」这一头
    # 由同一批里的英文检索词接住，不靠这一条包全。
    assert intent.queries == (
        "大模型输出水印 识别",
        "LLM watermark detection removal",
        "Claude output watermark")
    # 描述性的 subject 原样留着给排序和界面用，只是不拿去搜。
    assert intent.subject == "大模型输出水印的识别与去除"
    # 自己拼检索词的调用方（系列摸底）拿到的是能搜的那一份。
    assert intent.search_base == "大模型输出水印 识别"


def test_the_query_rule_and_its_example_reach_the_model():
    provider = RecordingProvider(['{"subject":"A","queries":["a"]}'])
    understand_topic("远程办公", "zh", provider)
    prompt = provider.prompts[0]

    assert "一条检索词只查一件事" in prompt
    assert "10 个汉字以内" in prompt
    assert "太粗：大模型输出水印的识别与去除技术" in prompt


def test_connectives_split_but_ordinary_words_that_contain_them_survive():
    assert query_terms("大模型输出水印的识别与去除技术") == [
        "大模型输出水印", "识别", "去除"]
    assert query_terms("扩散模型和自回归模型对比") == [
        "扩散模型", "自回归模型对比"]
    # 「参与」「目的」「和平」里的这几个字不是连接词，断在这儿就把词拆坏了。
    assert query_terms("公众参与机制") == ["公众参与机制"]
    assert query_terms("研究目的与方法") == ["研究目的"]
    assert query_terms("亚洲和平进程") == ["亚洲和平进程"]
    assert query_terms("的士司机收入") == ["的士司机收入"]


def test_vacuous_tails_are_dropped_and_short_queries_left_alone():
    assert atomize_query("RAG 检索 优化方法") == "RAG 检索 优化"
    assert atomize_query("watermark removal techniques") == "watermark removal"
    # 已经够原子的不动，包括摸底检索靠的那些「综述」「overview」限定词。
    for query in ("远程办公 效率 研究", "RAG 综述", "LLM watermark removal",
                  "reinforcement learning overview"):
        assert atomize_query(query) == query


def test_a_long_chinese_query_is_trimmed_from_the_tail():
    assert atomize_query("大模型 水印 检测 去除 部署 成本") == "大模型 水印 检测"
    assert atomize_query("强化学习 策略梯度 收敛性 证明") == "强化学习 策略梯度"
    # 一个字都断不开的长词只能原样送出去，切在词中间比长着更糟。
    assert atomize_query("大模型输出水印检测") == "大模型输出水印检测"


def test_shortening_keeps_the_leading_concept_and_then_gives_up():
    assert shorten_query("大模型输出水印 识别 去除") == "大模型输出水印 识别"
    assert shorten_query("大模型输出水印 识别") == "大模型输出水印"
    assert shorten_query("大模型输出水印") == ""
    assert shorten_query("LLM watermark removal") == "LLM watermark"
    assert shorten_query("") == ""


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
