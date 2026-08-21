"""术语该留英文还是写中文，一处说清，各写作提示词共用。

这件事栽过三次，每次都是同一种错——把规则写成了一边倒。

一、一概译成中文，于是 AI agent 被写成「AI 代理」。
二、改成「专业术语一律保留英文」，training、latency、open source 这些早有通行
    中文说法的词也留在句子中间，读起来像没译完。
三、改成按词判断、两个方向都说，仍旧漏了短语：规则按「词」写，模型按「短语」
    用。prompt 在留英文的名单里，于是 plain-language prompt 整个短语原封不动
    留在中文句子里——plain-language 是普通英文，该写成「大白话」。

所以规则现在写死两条：英文只留术语本身那一个词，修饰它、连接它的一律中文；
拿不准的时候默认译。规则说不清的部分交给成对的改法示例。

改写的系统提示词、改写指令、各风格的通用要求、综合写作的深度要求，四处都要
说同一套话，所以话放在这里，不在各自那边各写一遍。
"""

from __future__ import annotations

# 下面两份词表都只是举例，提示词里也这么标着。哪些词该译是个开放集合——换个
# 选题就换一批，列不全是它的常态，别指望靠加词把这件事做对。
#
# 早有通行中文说法的术语，留着英文就是没译完。第二次栽的就是这批词。
MUST_TRANSLATE: tuple[tuple[str, str], ...] = (
    ("training", "训练"),
    ("inference", "推理"),
    ("fine-tuning", "微调"),
    ("dataset", "数据集"),
    ("parameters", "参数"),
    ("weights", "权重"),
    ("latency", "延迟"),
    ("throughput", "吞吐"),
    ("open-source", "开源"),
    ("deployment", "部署"),
    ("quantization", "量化"),
    ("distillation", "蒸馏"),
    ("multimodal", "多模态"),
    ("benchmark", "基准测试"),
    ("accuracy", "准确率"),
)

# 普通词汇根本不是术语，留着英文没有任何理由。
PLAIN_WORDS: tuple[tuple[str, str], ...] = (
    ("release", "发布"),
    ("performance", "性能"),
    ("significant", "显著"),
    ("feature", "功能"),
    ("update", "更新"),
)

# 改法示例。规则讲不清「哪半截该译」，示例一看就懂；最后两条是反向的——
# 该留的也别乱动，免得又矫正到另一边去。
_EXAMPLES: tuple[tuple[str, str, str], ...] = (
    ("plain-language prompt", "大白话 prompt",
     "plain-language 是普通英文，只有 prompt 是术语"),
    ("inference latency", "推理延迟", "两个词都有通行中文说法"),
    ("long-context model", "长上下文模型", "整个短语都译"),
    ("agentic workflow", "agent 工作流", "agent 留着，workflow 要译"),
    ("state-of-the-art results", "当前最好的结果", "普通英文，别留"),
    ("human-in-the-loop review", "人工复核", "照字面译反而没人看懂"),
    ("GPT-4 Turbo", "GPT-4 Turbo", "产品名，原样不动"),
    ("system prompt", "system prompt", "中文圈就这么写，属于少数整段留英文的"),
)


def _pairs(items: tuple[tuple[str, str], ...]) -> str:
    return "、".join(f"{en} 写「{zh}」" for en, zh in items)


def _example_table() -> str:
    rows = "".join(f"| {bad} | {good} | {why} |\n" for bad, good, why in _EXAMPLES)
    return ("| 原文里的写法 | 该写成 | 为什么 |\n|---|---|---|\n" + rows)


TERM_RULE = (
    "### 术语的中英文\n\n"
    "像专业译者那样逐处判断：该译的译，不该译的不动。判断标准只有一条——"
    "中文技术圈平时写这个词，写的是中文还是英文？跟着他们写。"
    "拿不准就译成中文：留英文是例外，得说得出理由（是名字、是缩写、"
    "中文圈确实这么写）。\n"
    "- **留英文**：模型名、产品名、公司名（GPT-4、Claude、Llama）；缩写"
    "（LLM、RAG、MoE、API、SDK、GPU）；中文圈本来就直接说英文的词"
    "（agent、prompt、token、Transformer）。把 AI agent 写成「AI 代理」"
    "反而显得外行。\n"
    "- **用中文**：已经有通行中文说法的一律译过来，例如"
    + _pairs(MUST_TRANSLATE) + "。这几个只是举例——凡是中文里有说法的都算，"
    "不在这份例子里不等于可以留着英文。这类词留着英文不叫专业，叫没译完。\n"
    "- **普通词汇不是术语**，一律用中文，例如"
    + _pairs(PLAIN_WORDS) + "，同样只是举例。\n"
    "- **英文只留术语本身那一个词**，修饰它的、连接它的、跟它并列的普通英文"
    "全都要写成中文。一个术语在留英文的名单里，不等于带着它的整个短语都能"
    "留在英文里——这是最容易出错的地方。除了产品名（GPT-4 Turbo）和固定"
    "缩写，正文里连着两个以上的英文单词，基本都是没译完。\n"
    "- 译成中文的术语，第一次出现时可以在括号里标一下英文原词，例如"
    "「上下文窗口（context window）」，后文直接用中文。\n"
    "- 读者可能不认识的术语，第一次出现时用一句话说清它是什么、解决什么问题，"
    "再往下用。这句解释跟留不留英文是两件事，别拿「保留英文」当成不用解释。\n\n"
    + _example_table() + "\n"
    "写完自查一遍：正文里每一处英文都问一句「中文技术圈会这么写吗」。"
    "凡是连着两个以上英文单词、又不是产品名的地方，都改成中文。"
)

# 系统提示词和速查表格里塞不下整段规则，用这一句，完整规则由指令那边给。
TERM_RULE_LINE = (
    "术语按中文技术圈的习惯写：模型名、缩写和 agent、prompt 这类词留英文原文，"
    "已有通行中文说法的（训练、推理、微调、延迟、开源…）一律译成中文，"
    "普通词汇一律用中文；英文只留术语本身那一个词，修饰它的词要写成中文"
    "（plain-language prompt 写成「大白话 prompt」），拿不准就译。"
)
