from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone

from concurrent.futures import ThreadPoolExecutor, as_completed

from app.discovery import search_web
from app.feeds import FeedsConfig
from app.llm.base import LLMProvider, Message

# Built-in seeds so the recommender stays useful offline (mock provider) or
# when the LLM returns nothing parseable.
# Entries are ordered by estimated popularity / hotness (hotter first).
_SUBTOPIC_SEED: dict[str, list[str]] = {
    "科技": [
        "人工智能", "大语言模型", "具身智能", "机器人", "AI Agent",
        "脑机接口", "自动驾驶", "半导体芯片", "量子计算", "商业航天",
        "新能源", "生物科技", "AR/VR", "低空经济", "6G通信",
        "新材料", "核聚变", "AI芯片", "智能制造", "Web3",
    ],
    "ai": [
        "大语言模型", "多模态", "AI Agent", "具身智能", "AIGC",
        "视频生成", "AI编程", "AI搜索", "AI芯片", "世界模型",
        "强化学习", "AI安全", "AI For Science", "开源模型", "边缘AI",
        "模型压缩", "AI伦理", "RAG", "计算机视觉", "语音合成",
    ],
    "人工智能": [
        "大语言模型", "具身智能", "多模态", "AI Agent", "视频生成",
        "AIGC", "AI编程", "AI搜索", "世界模型", "AI芯片",
        "强化学习", "计算机视觉", "自然语言处理", "RAG", "AI安全",
        "AI For Science", "语音合成", "开源模型", "边缘AI", "模型压缩",
        "深度强化学习", "联邦学习", "可解释AI", "AI伦理", "AI Agent框架",
    ],
    "大语言模型": [
        "DeepSeek", "GPT系列", "Claude", "Gemini", "Qwen/千问",
        "LLaMA", "Mistral", "GLM/智谱", "Kimi", "长上下文",
        "多模态", "RAG", "Agent框架", "模型微调", "推理优化",
        "MoE架构", "量化", "蒸馏", "开源模型", "AI安全对齐",
        "思维链", "工具调用", "基准评测", "合成数据", "代码生成",
    ],
    "物理ai": [
        "具身智能", "人形机器人", "世界模型", "灵巧手", "运动控制",
        "仿真环境", "Sim-to-Real", "机器人操作", "自动驾驶", "机械臂",
        "遥操作", "抓取", "移动操作", "人机协作", "感知与导航",
    ],
    "机器人": [
        "人形机器人", "具身智能", "灵巧手", "运动控制", "机器人视觉",
        "SLAM", "机械臂", "四足机器人", "服务机器人", "工业机器人",
        "协作机器人", "自动驾驶", "无人机", "水下机器人", "软体机器人",
        "手术机器人", "仓储机器人", "配送机器人", "教育机器人", "机器人仿真",
    ],
    "脑机接口": [
        "侵入式BCI", "非侵入式BCI", "Neuralink", "脑电EEG", "神经信号解码",
        "闭环DBS", "视觉假体", "运动假体", "脑控", "神经调控",
        "感知神经", "脑机融合", "神经成像", "脑机接口芯片", "临床应用",
    ],
    "财经": [
        "宏观经济", "股市", "加密货币", "创业投资", "房地产",
        "量化交易", "Web3", "DeFi", "美联储政策", "ETF",
        "AI投资", "新能源投资", "半导体产业", "消费趋势", "跨境贸易",
        "黄金", "债券", "基金", "保险科技", "支付",
    ],
    "健康": [
        "AI医疗", "营养", "运动健身", "睡眠", "心理健康",
        "慢病管理", "基因编辑", "长寿科学", "疫苗", "肠道菌群",
        "中医现代化", "可穿戴设备", "数字疗法", "脑健康", "精准医疗",
        "细胞治疗", "mRNA技术", "医疗机器人", "健康管理", "抗衰老",
    ],
}

_SOURCE_SEED: dict[str, list[dict]] = {
    # Seed URLs are best-effort — the primary discovery mechanism uses web
    # search + RSS autodiscovery, so these only serve as fallback when search
    # is unavailable.  URLs are the company/org homepages (not guessed paths).
    "ai": [
        # --- International ---
        {"name": "OpenAI", "url": "https://openai.com/blog"},
        {"name": "Anthropic", "url": "https://www.anthropic.com/news"},
        {"name": "Google DeepMind", "url": "https://deepmind.google/discover/blog/"},
        {"name": "Meta AI", "url": "https://ai.meta.com/blog/"},
        {"name": "Mistral AI", "url": "https://mistral.ai/news/"},
        {"name": "xAI", "url": "https://x.ai/blog"},
        {"name": "Hugging Face", "url": "https://huggingface.co/blog"},
        {"name": "Microsoft Research", "url": "https://www.microsoft.com/en-us/research/blog/"},
        {"name": "NVIDIA", "url": "https://blogs.nvidia.com/blog/"},
        # --- Chinese ---
        {"name": "DeepSeek", "url": "https://www.deepseek.com"},
        {"name": "智谱AI (Zhipu/GLM)", "url": "https://zhipuai.cn"},
        {"name": "阿里千问 (Qwen)", "url": "https://qwenlm.github.io/blog"},
        {"name": "月之暗面 (Moonshot/Kimi)", "url": "https://platform.moonshot.cn"},
        {"name": "百川智能 (Baichuan)", "url": "https://www.baichuan-ai.com"},
        {"name": "MiniMax", "url": "https://www.minimaxi.com"},
        {"name": "零一万物 (01.AI/Yi)", "url": "https://www.lingyiwanwu.com"},
        {"name": "阶跃星辰 (Stepfun)", "url": "https://www.stepfun.com"},
        {"name": "百度 (文心一言)", "url": "https://yiyan.baidu.com"},
        {"name": "字节跳动 (豆包/火山引擎)", "url": "https://www.volcengine.com"},
    ],
    "人工智能": [
        # --- International ---
        {"name": "OpenAI", "url": "https://openai.com/blog"},
        {"name": "Anthropic", "url": "https://www.anthropic.com/news"},
        {"name": "Google DeepMind", "url": "https://deepmind.google/discover/blog/"},
        {"name": "Meta AI", "url": "https://ai.meta.com/blog/"},
        {"name": "Mistral AI", "url": "https://mistral.ai/news/"},
        {"name": "xAI", "url": "https://x.ai/blog"},
        {"name": "Hugging Face", "url": "https://huggingface.co/blog"},
        # --- Chinese ---
        {"name": "DeepSeek (深度求索)", "url": "https://www.deepseek.com"},
        {"name": "智谱AI (GLM)", "url": "https://zhipuai.cn"},
        {"name": "阿里千问 (Qwen/通义)", "url": "https://qwenlm.github.io/blog"},
        {"name": "月之暗面 (Kimi)", "url": "https://platform.moonshot.cn"},
        {"name": "百川智能 (Baichuan)", "url": "https://www.baichuan-ai.com"},
        {"name": "MiniMax (稀宇科技)", "url": "https://www.minimaxi.com"},
        {"name": "零一万物 (01.AI/Yi)", "url": "https://www.lingyiwanwu.com"},
        {"name": "阶跃星辰 (Stepfun)", "url": "https://www.stepfun.com"},
        {"name": "百度 (文心一言)", "url": "https://yiyan.baidu.com"},
        {"name": "字节跳动 (豆包/火山引擎)", "url": "https://www.volcengine.com"},
        {"name": "科大讯飞 (星火)", "url": "https://xinghuo.xfyun.cn"},
    ],
    "大语言模型": [
        {"name": "OpenAI", "url": "https://openai.com/blog"},
        {"name": "Anthropic", "url": "https://www.anthropic.com/news"},
        {"name": "DeepSeek", "url": "https://www.deepseek.com"},
        {"name": "智谱AI (GLM)", "url": "https://zhipuai.cn"},
        {"name": "阿里千问 (Qwen)", "url": "https://qwenlm.github.io/blog"},
        {"name": "月之暗面 (Kimi)", "url": "https://platform.moonshot.cn"},
        {"name": "Meta AI (LLama)", "url": "https://ai.meta.com/blog/"},
        {"name": "Mistral AI", "url": "https://mistral.ai/news/"},
        {"name": "xAI (Grok)", "url": "https://x.ai/blog"},
        {"name": "Google DeepMind (Gemini)", "url": "https://deepmind.google/discover/blog/"},
        {"name": "百川智能 (Baichuan)", "url": "https://www.baichuan-ai.com"},
        {"name": "MiniMax", "url": "https://www.minimaxi.com"},
        {"name": "零一万物 (Yi)", "url": "https://www.lingyiwanwu.com"},
        {"name": "Hugging Face", "url": "https://huggingface.co/blog"},
    ],
    "物理ai": [
        {"name": "Physical Intelligence", "url": "https://www.physicalintelligence.company"},
        {"name": "NVIDIA Robotics", "url": "https://developer.nvidia.com/isaac"},
        {"name": "Boston Dynamics", "url": "https://bostondynamics.com/blog/"},
        {"name": "Figure", "url": "https://www.figure.ai"},
        {"name": "Google DeepMind", "url": "https://deepmind.google/discover/blog/"},
        {"name": "Tesla Optimus", "url": "https://www.tesla.com/AI"},
        {"name": "Agility Robotics", "url": "https://agilityrobotics.com"},
        {"name": "1X Technologies", "url": "https://www.1x.tech"},
        {"name": "星动纪元 (Robot Era)", "url": "https://www.robo-era.com"},
        {"name": "宇树科技 (Unitree)", "url": "https://www.unitree.com"},
    ],
    "机器人": [
        {"name": "Boston Dynamics", "url": "https://bostondynamics.com/blog/"},
        {"name": "Figure", "url": "https://www.figure.ai"},
        {"name": "Agility Robotics", "url": "https://agilityrobotics.com"},
        {"name": "Physical Intelligence", "url": "https://www.physicalintelligence.company"},
        {"name": "Tesla Optimus", "url": "https://www.tesla.com/AI"},
        {"name": "1X Technologies", "url": "https://www.1x.tech"},
        {"name": "NVIDIA Robotics", "url": "https://developer.nvidia.com/isaac"},
        {"name": "宇树科技 (Unitree)", "url": "https://www.unitree.com"},
        {"name": "星动纪元 (Robot Era)", "url": "https://www.robo-era.com"},
        {"name": "智元机器人 (Agibot)", "url": "https://www.agibot.com"},
    ],
    "脑机接口": [
        {"name": "Neuralink", "url": "https://neuralink.com/blog/"},
        {"name": "Synchron", "url": "https://synchron.com"},
        {"name": "脑虎科技 (Neuralrobo)", "url": "https://www.neuralrobo.com"},
        {"name": "博睿康 (Neuracle)", "url": "https://www.neuracle.cn"},
    ],
}

_KEYWORD_SEED: dict[str, list[str]] = {
    "人工智能": ["AI", "大模型", "LLM", "GPT", "深度学习", "神经网络",
                 "AIGC", "多模态", "Transformer", "OpenAI", "训练", "推理",
                 "具身智能", "AI Agent", "AGI", "扩散模型", "强化学习",
                 "计算机视觉", "自然语言处理", "AI安全"],
    "aigc": ["AIGC", "AI生成", "文生图", "文生视频", "Sora", "Midjourney",
             "Stable Diffusion", "DALL-E", "AI音乐", "AI配音"],
    "大语言模型": ["LLM", "GPT", "Claude", "Gemini", "DeepSeek", "Qwen",
                   "上下文窗口", "微调", "RAG", "推理", "幻觉", "对齐",
                   "MoE", "量化", "蒸馏", "Agent", "function calling",
                   "长文本", "思维链", "RLHF"],
    "ai agent": ["AI Agent", "Agent框架", "function calling", "工具使用",
                 "AutoGPT", "CrewAI", "LangGraph", "多Agent", "规划",
                 "记忆", "反思", "Agent评测"],
    "具身智能": ["embodied AI", "具身智能", "人形机器人", "世界模型",
                 "灵巧手", "机械臂操作", "Sim-to-Real", "遥操作",
                 "Physical Intelligence", "Figure", "Tesla Optimus"],
    "脑机接口": ["BCI", "brain-computer interface", "Neuralink", "侵入式",
                 "非侵入式", "脑电", "EEG", "神经信号", "意念控制",
                 "Synchron", "闭环DBS", "神经调控"],
    "机器人": ["robotics", "人形机器人", "波士顿动力", "机械臂", "运动控制",
               "强化学习", "具身智能", "四足机器人", "灵巧手", "SLAM",
               "宇树", "Figure", "服务机器人", "工业机器人"],
    "自动驾驶": ["autonomous driving", "L4", "激光雷达", "BEV", "端到端",
                 "Tesla FSD", "Waymo", "萝卜快跑", "城市NOA", "占用网络"],
    "视频生成": ["视频生成", "Sora", "文生视频", "视频理解", "CogVideo",
                "Runway", "Pika", "可灵", "Vidu", "视频编辑"],
    "ai编程": ["AI编程", "Cursor", "GitHub Copilot", "Claude Code", "Devin",
               "代码生成", "补全", "重构", "代码审查", "自动调试"],
    "量子计算": ["quantum computing", "量子比特", "超导", "离子阱",
                 "量子纠错", "量子优势", "IBM Quantum", "Google Quantum"],
    "新能源": ["新能源", "光伏", "锂电池", "固态电池", "氢能",
               "钙钛矿", "储能", "新能源汽车", "充电桩", "核聚变"],
    "商业航天": ["商业航天", "SpaceX", "星舰", "Starlink", "可回收火箭",
                 "卫星互联网", "太空旅游", "民营航天"],
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
                      limit: int = 24) -> list[str]:
    themes = [t.strip() for t in themes if t.strip()]
    if not themes:
        return []

    # 1) Collect seed entries (curated, ordered by hotness)
    seed_items: list[str] = []
    for t in themes:
        seed_items.extend(_SUBTOPIC_SEED.get(t.lower(), []))

    # 2) Ask LLM for comprehensive + hotness-ordered list
    prompt = (
        "你是资深自媒体选题策划。用户给出一个或多个大主题，请全面列出这些主题下"
        "最热门、最值得持续关注的细分子主题（中文名词短语，具体、可检索）。\n"
        "要求：\n"
        "1. 覆盖越全越好，不要遗漏重要的子领域\n"
        "2. 按当前热度从高到低排序\n"
        "3. 至少输出 20 项\n"
        f"主题：{', '.join(themes)}\n"
        "只输出一个 JSON 字符串数组，不要任何额外说明。"
    )
    result = _parse_list(provider.chat([Message(role="user", content=prompt)]))

    # 3) Merge: seed（热点优先）+ LLM（补充新颖项），去重
    merged = list(seed_items)
    if result:
        merged.extend(result)
    if not merged:
        for t in themes:
            merged.extend([f"{t}前沿", f"{t}应用", f"{t}趋势",
                           f"{t}公司", f"{t}产品"])
    return _dedupe(merged, limit)


def suggest_keywords(subtopic: str, provider: LLMProvider,
                     limit: int = 20) -> list[str]:
    subtopic = subtopic.strip()
    if not subtopic:
        return []

    # 1) Seed entries first (curated, by importance)
    seed_items = list(_KEYWORD_SEED.get(subtopic.lower(), []))

    # 2) LLM for comprehensive coverage
    prompt = (
        "你是 SEO 与选题专家。针对给定子主题，全面列出用于检索和归类相关文章的"
        "关键词（中英文混合，覆盖核心术语、代表性公司/模型/技术/事件）。\n"
        "要求：\n"
        "1. 覆盖全部重要术语，不要遗漏\n"
        "2. 按重要性和搜索频次从高到低排序\n"
        "3. 至少输出 15 项\n"
        f"子主题：{subtopic}\n"
        "只输出一个 JSON 字符串数组，不要任何额外说明。"
    )
    result = _parse_list(provider.chat([Message(role="user", content=prompt)]))

    # 3) Merge: seed first + LLM extras
    merged = list(seed_items)
    if result:
        merged.extend(result)
    if not merged:
        merged = [subtopic, f"{subtopic}技术", f"{subtopic}应用",
                  f"{subtopic}公司", f"{subtopic}最新进展"]
    return _dedupe(merged, limit)


def suggest_sources(topic: str, provider: LLMProvider,
                    limit: int = 20) -> list[dict]:
    """Recommend frontier companies / orgs / media (with their news/blog URLs)
    for a topic, so the caller can auto-discover feeds from them.

    Uses a two-phase approach:
    1. Ask the LLM for well-known company/org names in the field
    2. For each name, use web search + RSS autodiscovery to find actual
       feed URLs (LLMs hallucinate URLs — they guess plausible-looking
       paths like /blog or /news that often return 404)
    """
    topic = topic.strip()
    if not topic:
        return []

    prompt = (
        "你是行业研究员。针对给定主题，列出该领域全球最前沿、最值得关注的"
        "公司 / 研究机构 / 实验室 / 行业媒体。\n"
        "\n"
        "重要：无论什么主题，中外来源都要兼顾，至少一半应为国内来源"
        "（包含国内公司、国内研究机构、国内行业媒体）。\n"
        "\n"
        f"主题：{topic}\n"
        '只输出 JSON 数组，每个元素形如 '
        '{"name": "OpenAI", "url": "https://openai.com/news/"}，'
        "url 尽量给出该机构发布新闻或博客的官方主页（不确定可留空）。"
        "不要任何额外说明。"
    )
    llm_items = _parse_objects(provider.chat([Message(role="user", content=prompt)]))

    from app.discovery import discover_from_keyword, discover_from_url

    out: list[dict] = []
    seen: set[str] = set()

    def _try_discover(name: str, url: str) -> bool:
        """Try to find a real RSS feed for *name* via URL discovery or search."""
        # 1) If LLM gave a URL, try RSS autodiscovery from that page
        if url:
            try:
                for src in discover_from_url(url):
                    if src.url not in seen and src.type == "rss":
                        seen.add(src.url)
                        out.append({"name": name, "url": src.url})
                        return True
            except Exception:
                pass

        # 2) Search DuckDuckGo for "{name} blog" → discover RSS from top hits
        for query in (f"{name} blog", f"{name}"):
            try:
                for src in discover_from_keyword(query, max_sites=2):
                    if src.url not in seen and src.type == "rss":
                        seen.add(src.url)
                        out.append({"name": name, "url": src.url})
                        return True
            except Exception:
                pass
        return False

    # Phase 2a: process LLM suggestions (discover real RSS feeds)
    for item in llm_items:
        if len(out) >= limit:
            break
        name = item.get("name", "")
        url = item.get("url", "")
        if name:
            _try_discover(name, url)

    # Phase 2b: seed data fallback (try discovery, then add URL as best-effort)
    if not out:
        for item in _SOURCE_SEED.get(topic.lower(), []):
            if len(out) >= limit:
                break
            name, url = item["name"], item["url"]
            if not name or not url or any(o["name"] == name for o in out):
                continue
            if not _try_discover(name, url) and url not in seen:
                seen.add(url)
                out.append({"name": name, "url": url})

    return out


def _parse_hotness_json(raw: str) -> dict:
    """Extract {'topics': {...}, 'sources': {...}} from an LLM response."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {}


def compute_hotness(
    store,
    feeds_cfg: FeedsConfig | None = None,
    provider: LLMProvider | None = None,
) -> dict:
    """Hotness ranking with two tiers:

    **Real LLM** (non-mock provider):
        1. LLM baseline score (0–100) from training data (web-scale awareness)
        2. DDG freshness bonus (±0–45) via parallel search result count
        3. Local article stats for display context
    **Mock / offline**:
        1. Local article volume as baseline
        2. DDG freshness still applied (optional real web signal)
        3. Falls back to pure local if DDG unavailable

    Returns
    -------
    {"topics": [...], "sources": [...], "updated_at": "...", "source": str}
    Each entry: {name, score, total, recent, drafts} sorted by score desc.
    """
    cutoff_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ── Phase 0: local article stats (always) ──────────────────────────
    articles = store.conn.execute(
        "SELECT topic, source_name, published_at, fetched_at FROM articles"
    ).fetchall()

    t_stats: dict[str, dict] = defaultdict(
        lambda: {"total": 0, "recent": 0, "drafts": 0})
    s_stats: dict[str, dict] = defaultdict(
        lambda: {"total": 0, "recent": 0})

    for a in articles:
        topic = (a["topic"] or "未分类").strip()
        topic = topic if topic else "未分类"
        src = a["source_name"] or "(未知来源)"

        ts_str = a["published_at"] or a["fetched_at"]
        is_recent = bool(ts_str and ts_str[:10] >= cutoff_date)

        t_stats[topic]["total"] += 1
        if is_recent:
            t_stats[topic]["recent"] += 1

        s_stats[src]["total"] += 1
        if is_recent:
            s_stats[src]["recent"] += 1

    drafts = store.conn.execute(
        """SELECT a.topic, COUNT(*) as cnt
           FROM drafts d JOIN articles a ON d.article_id = a.id
           WHERE a.topic IS NOT NULL
           GROUP BY a.topic"""
    ).fetchall()
    for d in drafts:
        topic = (d["topic"] or "未分类").strip() or "未分类"
        t_stats[topic]["drafts"] = d["cnt"]

    # Collect configured names (feeds_cfg) or derive from store
    topic_names: list[str] = (
        [t.name for t in feeds_cfg.topics]
        if feeds_cfg and feeds_cfg.topics
        else sorted(t_stats.keys())
    )
    source_names: list[str] = (
        [s.name for s in feeds_cfg.sources]
        if feeds_cfg and feeds_cfg.sources
        else sorted(s_stats.keys())
    )

    # ── Phase 1: LLM baseline (one API call) ──────────────────────────
    llm_topic_scores: dict[str, int] = {}
    llm_source_scores: dict[str, int] = {}
    source_type = "local"

    is_mock = provider is None or type(provider).__name__ == "MockProvider"
    if not is_mock and (topic_names or source_names):
        parts: list[str] = []
        if topic_names:
            parts.append(
                "主题（按全球/中文互联网整体热度评分）：\n"
                + "\n".join(f"- {n}" for n in topic_names)
            )
        if source_names:
            parts.append(
                "来源（按该机构近期受关注程度评分）：\n"
                + "\n".join(f"- {n}" for n in source_names)
            )
        prompt = (
            "你是互联网热点分析师。请根据你了解的近期网络趋势"
            "（新闻覆盖度、社交媒体讨论热度、搜索热度等），"
            "对以下项目分别进行 0-100 的热度评分。\n"
            "评分越高代表当前越热门，越低代表热度低或已过气。\n\n"
            + "\n\n".join(parts)
            + "\n\n只返回 JSON 格式，不要额外文字：\n"
            '{"topics": {"主题A": 95, ...}, "sources": {"来源A": 80, ...}}'
        )
        try:
            raw = provider.chat([Message(role="user", content=prompt)])
            parsed = _parse_hotness_json(raw)
            llm_topic_scores = {
                k: max(0, min(100, int(v)))
                for k, v in parsed.get("topics", {}).items()
            }
            llm_source_scores = {
                k: max(0, min(100, int(v)))
                for k, v in parsed.get("sources", {}).items()
            }
            source_type = "llm+web"
        except Exception:
            pass

    # ── Phase 2: DDG freshness boost (topics only, parallel) ──────────
    ddg_scores: dict[str, int] = {}

    def _ddg_freshness(name: str) -> int:
        try:
            results = search_web(name, max_results=15)
            return min(len(results), 15) * 3  # max 45
        except Exception:
            return 0

    if topic_names:
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_ddg_freshness, n): n for n in topic_names}
            # 30s total wall clock budget for all DDG searches
            try:
                for f in as_completed(futures, timeout=30):
                    name = futures[f]
                    try:
                        ddg_scores[name] = f.result()
                    except Exception:
                        ddg_scores[name] = 0
            except TimeoutError:
                for f in futures:
                    f.cancel()

    # ── Phase 3: combine scores ────────────────────────────────────────
    def _topic_score(name: str) -> int:
        stats = t_stats.get(name, {"total": 0, "recent": 0, "drafts": 0})
        if llm_topic_scores:
            base = llm_topic_scores.get(name, 30)
        else:
            base = min(stats["total"] * 2, 50)
        bonus = ddg_scores.get(name, 0)
        return min(base + bonus, 100)

    topics = []
    for n in topic_names:
        stats = t_stats.get(n, {"total": 0, "recent": 0, "drafts": 0})
        topics.append({
            "name": n,
            "score": _topic_score(n),
            "total": stats["total"],
            "recent": stats["recent"],
            "drafts": stats["drafts"],
        })
    topics.sort(key=lambda x: (-x["score"], -x["total"]))

    def _source_score(name: str) -> int:
        stats = s_stats.get(name, {"total": 0, "recent": 0})
        if llm_source_scores:
            base = llm_source_scores.get(name, 30)
        else:
            base = min(stats["total"] * 2, 50)
        return min(base, 100)

    sources = []
    for n in source_names:
        stats = s_stats.get(n, {"total": 0, "recent": 0})
        sources.append({
            "name": n,
            "score": _source_score(n),
            "total": stats["total"],
            "recent": stats["recent"],
        })
    sources.sort(key=lambda x: (-x["score"], -x["total"]))

    return {
        "topics": topics,
        "sources": sources,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": source_type,
    }
