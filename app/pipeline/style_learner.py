"""Learn a reusable rewrite style from 3–10 target articles."""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from app.llm.base import LLMProvider, Message
from app.pipeline import styles
from app.pipeline.rewriter import _extract_json
from app.sources.scraper import scrape_single

_ANALYSIS_FIELDS = (
    "tone", "sentence_structure", "opening_hook", "paragraph_layout",
    "rhetoric", "title_pattern", "ending_pattern",
)

# 这里学出来的 guidance 和 system_prompt 最后是当写作提示词用的（见
# styles.py），而防 AI 腔的规则是在使用时追加到它后面的。样例要是满篇「随着
# ……的发展」，照实归纳出来的规则就成了「开篇用宏大背景铺垫」，跟后面那条
# 规则正面顶牛——而 guidance 写得更具体，顶牛时往往是它赢。所以在归纳这一
# 步就把套话挡在规则之外：要学的是这些文章怎么把话说清楚，不是它们的口癖。
_SYSTEM_PROMPT = (
    "你是写作风格分析专家。你的任务是从多篇目标文章中提取可复用的表达风格，"
    "只分析写法，不总结或复制文章事实。输出必须是严格 JSON。\n\n"
    "样例里的套话不算风格：宏大开头（「随着……的发展」「近年来」）、对仗式"
    "假深刻（「不仅是……更是……」）、报告腔（「赋能」「闭环」）、流量词"
    "（「深入浅出」「一文读懂」）、假口语（「说实话」「不得不说」）、空洞"
    "升华结尾、通篇破折号和加粗——这些是模型和公众号的共同口癖，不是这几篇"
    "文章的特点。不要把它们写进任何字段，尤其不要写进 style_guidance 和 "
    "system_prompt。要提炼的是这些文章真正有辨识度的地方：怎么起头、怎么"
    "推进、句子长短怎么搭、用什么具体材料支撑。"
)


def _validate_public_url(url: str) -> str:
    value = (url or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"样例 URL 无效：{value or '空 URL'}")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith((".local", ".internal")):
        raise ValueError("样例 URL 不能指向本机或内网地址")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_reserved or address.is_unspecified
    ):
        raise ValueError("样例 URL 不能指向本机或内网地址")
    return value


def collect_samples(raw_samples: list[dict], *,
                    proxy: str | None = None) -> list[dict]:
    """Validate pasted samples and fetch URL samples without persisting them."""
    if not isinstance(raw_samples, list) or not 3 <= len(raw_samples) <= 10:
        raise ValueError("请提供 3-10 篇目标风格文章")
    collected: list[dict] = []
    for index, raw in enumerate(raw_samples, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 篇样例格式无效")
        url = str(raw.get("url") or "").strip()
        content = str(raw.get("content") or "").strip()
        title = str(raw.get("title") or "").strip()
        if url:
            safe_url = _validate_public_url(url)
            article = scrape_single(
                safe_url, "风格学习样例", render_js=False, proxy=proxy)
            if article is None or not (article.content_md or "").strip():
                raise ValueError(f"第 {index} 篇 URL 未抓取到有效正文")
            title = article.title or title or f"样例 {index}"
            content = article.content_md
            url = safe_url
        if len(content) < 100:
            raise ValueError(f"第 {index} 篇样例正文过短（至少 100 字）")
        collected.append({
            "title": (title or f"样例 {index}")[:200],
            "url": url[:2000],
            "content": content[:12000],
        })
    return collected


def _analysis_prompt(samples: list[dict]) -> str:
    blocks: list[str] = []
    for index, sample in enumerate(samples, 1):
        blocks.append(
            f"### 样例 {index}：{sample['title']}\n"
            f"{sample['content'][:8000]}"
        )
    schema = (
        '{\n'
        '  "suggested_name": "简短中文风格名",\n'
        '  "description": "一句话描述",\n'
        '  "tone": "语气语调",\n'
        '  "sentence_structure": "句式结构",\n'
        '  "opening_hook": "开篇钩子模式",\n'
        '  "paragraph_layout": "段落布局",\n'
        '  "rhetoric": "常用修辞手法",\n'
        '  "title_pattern": "标题模式",\n'
        '  "ending_pattern": "结尾套路",\n'
        '  "system_prompt": "给改写模型的角色和语气系统提示词",\n'
        '  "style_guidance": "完整、可执行的写作风格规则"\n'
        '}'
    )
    return (
        "分析下面这些文章共有且稳定的写作风格。忽略具体主题和事实差异，"
        "不要模仿作者身份，不要引用样例内容。\n\n"
        "请覆盖语气、句式、开篇、段落、修辞、标题和结尾，并输出以下严格 JSON：\n"
        f"{schema}\n\n" + "\n\n---\n\n".join(blocks)
    )


def analyse_samples(samples: list[dict], provider: LLMProvider) -> dict:
    raw = provider.chat([
        Message(role="system", content=_SYSTEM_PROMPT),
        Message(role="user", content=_analysis_prompt(samples)),
    ])
    result = _extract_json(raw)
    if not isinstance(result, dict):
        raise ValueError("AI 未返回有效的风格分析 JSON，请重试")
    guidance = str(result.get("style_guidance") or "").strip()
    if not guidance:
        parts = [
            f"{field}：{str(result.get(field) or '').strip()}"
            for field in _ANALYSIS_FIELDS if result.get(field)
        ]
        guidance = "\n".join(parts)
    if not guidance:
        raise ValueError("AI 返回的风格分析缺少可执行指引")
    result["style_guidance"] = guidance
    return result


def learn_style(store, provider: LLMProvider, raw_samples: list[dict], *,
                name: str = "", proxy: str | None = None):
    """Collect samples, analyse them, and persist a learned custom style."""
    samples = collect_samples(raw_samples, proxy=proxy)
    analysis = analyse_samples(samples, provider)
    style_name = (name or str(analysis.get("suggested_name") or "")).strip()
    if not style_name:
        style_name = "学习风格"
    description = str(analysis.get("description") or "").strip()
    prompt = str(analysis.get("system_prompt") or "").strip()
    if not prompt:
        prompt = (
            f"你是一位擅长「{style_name}」的中文编辑。"
            "只改变表达方式，不改变原文事实。"
        )
    examples = [
        {"title": sample["title"], "content": sample["content"][:4000],
         "url": sample["url"]}
        for sample in samples[:2]
    ]
    learned_from = [
        {"title": sample["title"], "url": sample["url"]}
        for sample in samples
    ]
    return styles.save_custom_style(
        store, id=None, name=style_name, description=description,
        prompt=prompt,
        instruction=styles.build_learned_instruction(
            analysis["style_guidance"]),
        examples=examples, learned_from=learned_from,
        origin="learned", analysis={
            field: str(analysis.get(field) or "").strip()
            for field in _ANALYSIS_FIELDS
        },
    )
