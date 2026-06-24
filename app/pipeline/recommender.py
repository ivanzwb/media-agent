from __future__ import annotations

import json
import re

from app.llm.base import LLMProvider, Message

# Built-in seeds so the recommender stays useful offline (mock provider) or
# when the LLM returns nothing parseable.
_SUBTOPIC_SEED: dict[str, list[str]] = {
    "科技": ["人工智能", "脑机接口", "机器人", "半导体芯片", "量子计算",
             "新能源", "航天", "生物科技", "AR/VR", "自动驾驶"],
    "ai": ["大语言模型", "多模态", "AI Agent", "具身智能", "AIGC",
           "AI 芯片", "强化学习", "AI 安全"],
    "人工智能": ["大语言模型", "多模态", "AI Agent", "具身智能", "AIGC",
                 "AI 芯片", "强化学习", "AI 安全"],
    "物理ai": ["机器人", "具身智能", "世界模型", "自动驾驶", "机械臂操作",
               "仿真环境"],
    "财经": ["宏观经济", "股市", "加密货币", "创业投资", "房地产"],
    "健康": ["营养", "运动健身", "睡眠", "心理健康", "慢病管理"],
}

_SOURCE_SEED: dict[str, list[dict]] = {
    "ai": [
        {"name": "OpenAI", "url": "https://openai.com/news/"},
        {"name": "Anthropic", "url": "https://www.anthropic.com/news"},
        {"name": "Google DeepMind", "url": "https://deepmind.google/discover/blog/"},
        {"name": "Meta AI", "url": "https://ai.meta.com/blog/"},
        {"name": "Mistral AI", "url": "https://mistral.ai/news/"},
        {"name": "Hugging Face", "url": "https://huggingface.co/blog"},
        {"name": "Microsoft Research", "url": "https://www.microsoft.com/en-us/research/blog/"},
        {"name": "NVIDIA", "url": "https://blogs.nvidia.com/"},
    ],
    "人工智能": [
        {"name": "OpenAI", "url": "https://openai.com/news/"},
        {"name": "Anthropic", "url": "https://www.anthropic.com/news"},
        {"name": "Google DeepMind", "url": "https://deepmind.google/discover/blog/"},
        {"name": "Meta AI", "url": "https://ai.meta.com/blog/"},
        {"name": "Hugging Face", "url": "https://huggingface.co/blog"},
    ],
    "物理ai": [
        {"name": "Physical Intelligence", "url": "https://www.physicalintelligence.company/blog"},
        {"name": "NVIDIA Robotics", "url": "https://blogs.nvidia.com/blog/category/auto/"},
        {"name": "Boston Dynamics", "url": "https://bostondynamics.com/blog/"},
        {"name": "Figure", "url": "https://www.figure.ai/news"},
        {"name": "Google DeepMind", "url": "https://deepmind.google/discover/blog/"},
    ],
    "机器人": [
        {"name": "Boston Dynamics", "url": "https://bostondynamics.com/blog/"},
        {"name": "Figure", "url": "https://www.figure.ai/news"},
        {"name": "Agility Robotics", "url": "https://www.agilityrobotics.com/content"},
        {"name": "Physical Intelligence", "url": "https://www.physicalintelligence.company/blog"},
    ],
    "脑机接口": [
        {"name": "Neuralink", "url": "https://neuralink.com/blog/"},
        {"name": "Synchron", "url": "https://synchron.com/newsroom"},
    ],
}

_KEYWORD_SEED: dict[str, list[str]] = {
    "人工智能": ["AI", "大模型", "LLM", "GPT", "深度学习", "神经网络",
                 "AIGC", "多模态", "Transformer", "OpenAI", "训练", "推理"],
    "大语言模型": ["LLM", "GPT", "Claude", "Gemini", "上下文窗口", "微调",
                   "RAG", "推理", "幻觉", "对齐"],
    "脑机接口": ["BCI", "brain-computer interface", "Neuralink", "侵入式",
                 "非侵入式", "脑电", "EEG", "神经信号", "意念控制"],
    "机器人": ["robotics", "人形机器人", "波士顿动力", "机械臂", "运动控制",
               "强化学习", "具身智能"],
    "自动驾驶": ["autonomous driving", "L4", "激光雷达", "BEV", "端到端",
                 "Tesla FSD", "Waymo"],
}


def _parse_list(raw: str) -> list[str]:
    """Extract a JSON array of strings from a model reply, tolerantly."""
    def _coerce(obj) -> list[str]:
        if isinstance(obj, list):
            return [str(x).strip() for x in obj if str(x).strip()]
        return []

    try:
        return _coerce(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if m:
        try:
            return _coerce(json.loads(m.group(0)))
        except json.JSONDecodeError:
            pass
    return []


def _parse_objects(raw: str) -> list[dict]:
    """Extract a JSON array of {name, url} objects from a model reply."""
    def _coerce(obj) -> list[dict]:
        out: list[dict] = []
        if isinstance(obj, list):
            for x in obj:
                if not isinstance(x, dict):
                    continue
                url = str(x.get("url") or x.get("site") or x.get("link")
                          or "").strip()
                name = str(x.get("name") or x.get("org")
                           or x.get("title") or "").strip()
                if url.startswith("http"):
                    out.append({"name": name or url, "url": url})
        return out

    try:
        return _coerce(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if m:
        try:
            return _coerce(json.loads(m.group(0)))
        except json.JSONDecodeError:
            pass
    return []


def _dedupe(items: list[str], limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        key = it.strip().lower()
        if it.strip() and key not in seen:
            seen.add(key)
            out.append(it.strip())
        if len(out) >= limit:
            break
    return out


def suggest_subtopics(themes: list[str], provider: LLMProvider,
                      limit: int = 12) -> list[str]:
    themes = [t.strip() for t in themes if t.strip()]
    if not themes:
        return []

    prompt = (
        "你是资深自媒体选题策划。用户给出一个或多个大主题，请列出这些主题下"
        "值得持续产出内容的细分子主题（中文名词短语，具体、可检索）。\n"
        "例如「科技」可给出：人工智能、脑机接口、量子计算 等。\n"
        f"主题：{', '.join(themes)}\n"
        "只输出一个 JSON 字符串数组，不要任何额外说明。"
    )
    result = _parse_list(provider.chat([Message(role="user", content=prompt)]))

    if not result:
        for t in themes:
            result.extend(_SUBTOPIC_SEED.get(t.lower(), [
                f"{t}前沿", f"{t}应用", f"{t}趋势", f"{t}公司", f"{t}产品"]))
    return _dedupe(result, limit)


def suggest_keywords(subtopic: str, provider: LLMProvider,
                     limit: int = 15) -> list[str]:
    subtopic = subtopic.strip()
    if not subtopic:
        return []

    prompt = (
        "你是 SEO 与选题专家。针对给定子主题，列出用于检索和归类相关文章的"
        "关键词（中英文混合，覆盖核心术语、代表性公司/模型/技术/事件）。\n"
        f"子主题：{subtopic}\n"
        "只输出一个 JSON 字符串数组，不要任何额外说明。"
    )
    result = _parse_list(provider.chat([Message(role="user", content=prompt)]))

    if not result:
        result = _KEYWORD_SEED.get(subtopic.lower(), [
            subtopic, f"{subtopic}技术", f"{subtopic}应用",
            f"{subtopic}公司", f"{subtopic}最新进展"])
    return _dedupe(result, limit)


def suggest_sources(topic: str, provider: LLMProvider,
                    limit: int = 8) -> list[dict]:
    """Recommend frontier companies / orgs (with their news/blog URLs)
    for a topic, so the caller can auto-discover feeds from them."""
    topic = topic.strip()
    if not topic:
        return []

    prompt = (
        "你是行业研究员。针对给定主题，列出该领域全球最前沿、最值得关注的"
        "公司 / 研究机构 / 实验室，并给出它们发布新闻或博客的官方网址"
        "（尽量是 blog / news / research 页面）。\n"
        f"主题：{topic}\n"
        '只输出 JSON 数组，每个元素形如 '
        '{"name": "OpenAI", "url": "https://openai.com/news/"}，'
        "不要任何额外说明。"
    )
    result = _parse_objects(provider.chat([Message(role="user", content=prompt)]))

    if not result:
        result = list(_SOURCE_SEED.get(topic.lower(), []))

    out: list[dict] = []
    seen: set[str] = set()
    for item in result:
        url = item["url"].rstrip("/")
        if url and url not in seen:
            seen.add(url)
            out.append({"name": item["name"], "url": item["url"]})
        if len(out) >= limit:
            break
    return out
