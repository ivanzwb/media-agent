from __future__ import annotations

import json
import logging
import re

from app.llm.base import LLMProvider, Message
from app.models import Article

logger = logging.getLogger(__name__)

# How many articles to pack into ONE batched relevance-gate call. Per-article
# CLI-agent calls pay a large cold-start cost and time out under fan-out (the
# same problem already fixed in the recommender), so we classify a chunk of
# articles per call. Small chunks keep each call's output tiny and let a single
# failed/oversized chunk fall back to KEEP without affecting the others.
_RELEVANCE_CHUNK = 8

# How many characters of body content to show the model per article. Enough to
# tell a genuine article from a nav/marketing/about page without bloating the
# prompt.
_SNIPPET_CHARS = 300


def _parse_indices(raw: str) -> list[int] | None:
    """Extract a JSON array of integer indices from a model reply, tolerantly.

    Returns the parsed list of ints (which may be empty — a legitimate
    "drop everything in this chunk"), or ``None`` when NO JSON array can be
    found at all. ``None`` is the fail-open signal: the caller keeps the whole
    chunk rather than dropping articles on an unparseable / error response.
    """
    def _coerce(obj) -> list[int] | None:
        if not isinstance(obj, list):
            return None
        out: list[int] = []
        for x in obj:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                # Tolerate objects like {"index": 3} or "3"
                if isinstance(x, dict):
                    for key in ("index", "idx", "i", "id"):
                        if key in x:
                            try:
                                out.append(int(x[key]))
                                break
                            except (TypeError, ValueError):
                                pass
        return out

    try:
        parsed = _coerce(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if parsed is not None:
        return parsed

    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if m:
        try:
            return _coerce(json.loads(m.group(0)))
        except json.JSONDecodeError:
            return None
    return None


def _snippet(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(text.split())[:_SNIPPET_CHARS]


def _classify_chunk(chunk: list[Article], provider: LLMProvider, *,
                    content_scope: str = "news") -> list[Article]:
    """Classify ONE chunk. Returns the kept subset. FAIL-OPEN on any error."""
    lines = []
    for i, art in enumerate(chunk):
        lines.append(
            f"[{i}] 标题：{art.title}\n"
            f"    链接：{art.url}\n"
            f"    摘要：{_snippet(art.content_md or art.raw_summary)}"
        )
    listing = "\n".join(lines)
    if content_scope == "informational":
        keep = (
            "应【保留】：有实质信息的新闻、研究、科普、专业指南、医生/专家建议、"
            "深度文章、评测分析和经验解释。即使不是近期新闻，只要能为当前选题"
            "提供事实、风险、原因或建议，也应保留。\n"
        )
        role = "你是资深资料研究编辑"
    else:
        keep = (
            "应【保留】：具体的新闻、发布/更新公告、行业动态、研究与技术文章、"
            "深度报道、评测分析。\n"
        )
        role = "你是资深新闻内容审核编辑"
    prompt = (
        f"{role}。下面是一批抓取到的候选网页，请判断每一条是否为一篇"
        "有独立正文、值得作为创作资料保留的内容。\n"
        f"{keep}"
        "应【丢弃】：公司主页/关于我们/产品介绍/营销落地页/招聘/联系我们/"
        "版权声明/导航页/栏目列表页/索引页/纯目录等非文章内容。\n"
        "注意：这是判断【内容类型】，不是判断主题是否相关。\n\n"
        f"候选列表（共 {len(chunk)} 条）：\n{listing}\n\n"
        "只输出一个 JSON 数组，包含应保留条目的编号（方括号里的数字），"
        "例如 [0, 2, 3]。若全部应丢弃则输出 []。不要任何额外说明。"
    )
    try:
        raw = provider.chat([Message(role="user", content=prompt)])
    except Exception as exc:  # noqa: BLE001 — fail-open on provider errors
        logger.warning("relevance: chunk classify failed (%s) — keeping all", exc)
        return list(chunk)

    keep_idx = _parse_indices(raw)
    if keep_idx is None:
        logger.warning("relevance: unparseable chunk reply — keeping all")
        return list(chunk)

    valid = {i for i in keep_idx if 0 <= i < len(chunk)}
    return [art for i, art in enumerate(chunk) if i in valid]


def filter_relevant(articles: list[Article], provider: LLMProvider, *,
                    enabled: bool, progress=None,
                    content_scope: str = "news") -> list[Article]:
    """LLM quality gate for news feeds or broader informational search results.

    Both scopes drop marketing / product / navigation / about / listing /
    boilerplate pages. ``informational`` additionally keeps substantive
    explainers, professional guidance and science/health articles.

    Runs as BATCHED ``provider.chat`` calls (``_RELEVANCE_CHUNK`` articles per
    call) to avoid the per-article CLI-agent timeout problems. FAIL-OPEN: any
    chunk whose call errors, times out, or returns unparseable output is kept
    in full — an LLM hiccup never silently drops everything.

    When *enabled* is falsy, this is a passthrough (returns *articles* as-is).
    """
    if not enabled or not articles:
        return articles

    kept: list[Article] = []
    for start in range(0, len(articles), _RELEVANCE_CHUNK):
        chunk = articles[start:start + _RELEVANCE_CHUNK]
        kept.extend(_classify_chunk(
            chunk, provider, content_scope=content_scope))

    dropped = len(articles) - len(kept)
    logger.info("relevance filter: kept %d / dropped %d of %d",
                len(kept), dropped, len(articles))
    if progress:
        progress(f"  相关性过滤：保留 {len(kept)} 篇、丢弃 {dropped} 篇"
                 "（非新闻/行业动态/研究文章）")
    return kept
