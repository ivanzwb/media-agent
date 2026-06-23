from __future__ import annotations

from app.llm.base import LLMProvider, Message
from app.pipeline.rewriter import _extract_json

PLATFORMS = {
    "wechat": (
        "你是微信公众号资深编辑。把给定的主稿改写成适合公众号的长图文："
        "结构清晰、有小标题、段落稍长、专业但通俗，适合深度阅读。"
    ),
    "xiaohongshu": (
        "你是小红书爆款写手。把主稿改写成小红书风格："
        "标题强冲击力、多 emoji、短句、分点、口语化，结尾配 3-6 个话题标签（#xxx）。"
    ),
    "zhihu": (
        "你是知乎专栏作者。把主稿改写成知乎深度回答风格："
        "有观点、有逻辑递进、引用主稿中的事实佐证，理性克制、信息密度高。"
    ),
}

_INSTRUCTION = (
    "请基于且仅基于下面的主稿内容改写，不得编造主稿没有的事实、数据或结论。\n"
    "输出严格 JSON：{{\"title_candidates\": [2-3 个标题], "
    "\"body_md\": \"Markdown 正文\"}}，不要输出 JSON 以外内容。\n\n"
    "原标题：{title}\n\n主稿：\n{master}"
)


def adapt(master_md: str, title: str, platform: str,
          provider: LLMProvider) -> dict:
    if platform not in PLATFORMS:
        raise ValueError(f"Unknown platform: {platform}")
    system = PLATFORMS[platform]
    prompt = _INSTRUCTION.format(title=title, master=master_md[:6000])
    raw = provider.chat([
        Message(role="system", content=system),
        Message(role="user", content=prompt),
    ])
    parsed = _extract_json(raw)
    if parsed and parsed.get("title_candidates") and parsed.get("body_md"):
        return {"title_candidates": parsed["title_candidates"],
                "body_md": parsed["body_md"]}
    return {"title_candidates": [title], "body_md": raw}
