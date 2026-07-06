from __future__ import annotations

import re

from app.feeds import Topic
from app.llm.base import LLMProvider, Message
from app.models import Article

UNCATEGORIZED = "uncategorized"

# Keywords shorter than this get word-boundary matching to avoid false
# positives from substring matches (e.g. "AR" matching "large").
_SHORT_KW_LEN = 4


def _keyword_match(article: Article, topics: list[Topic]) -> str | None:
    haystack = f"{article.title}\n{article.content_md}".lower()
    for topic in topics:
        for kw in topic.keywords:
            pattern = re.escape(kw.lower())
            # Short keywords must match as whole words to avoid false positives
            # like "AR" matching "large", "start", "required" etc.
            if len(kw) <= _SHORT_KW_LEN:
                pattern = r"\b" + pattern + r"\b"
            if re.search(pattern, haystack):
                return topic.name
    return None


def _clean_reply(reply: str) -> str:
    """Strip common agent wrappers (quotes, markdown, reasoning prefixes)."""
    cleaned = reply.strip()
    # Strip surrounding quotes: "AI" 「AI」 'AI' 「AI」
    cleaned = re.sub(r'^[\u201c\u201d"\'"\u300c]\s*', '', cleaned)
    cleaned = re.sub(r'\s*[\u201d\u201c"\'"\u300d]$', '', cleaned)
    # Strip leading reasoning patterns like "根据内容，我认为属于 AI 类"
    # or "I think this belongs to AI category"
    for sep in ('：', ':', '属于', 'is '):
        if sep in cleaned:
            _, after = cleaned.rsplit(sep, 1)
            if after.strip():
                cleaned = after.strip()
    # Strip trailing punctuation that isn't part of the name
    cleaned = cleaned.rstrip('.,;!?。，；！？')
    # Strip bold markers
    cleaned = cleaned.strip('*')
    return cleaned.strip()


def classify(article: Article, topics: list[Topic],
             provider: LLMProvider) -> str:
    matched = _keyword_match(article, topics)
    if matched:
        return matched

    names = [t.name for t in topics]
    prompt = (
        "You are a topic classifier. Choose exactly ONE topic name from this "
        f"list that best fits the article, or reply 'none'.\nTopics: {names}\n\n"
        f"Title: {article.title}\n\nContent:\n{article.content_md[:1500]}\n\n"
        "Reply with only the topic name."
    )
    reply = provider.chat([Message(role="user", content=prompt)]).strip()

    # 1) Exact match (fast path, backward-compatible)
    for name in names:
        if name.lower() == reply.lower():
            return name

    # 2) Fuzzy match: strip wrapper text and search for topic name
    cleaned = _clean_reply(reply)
    for name in names:
        if name.lower() == cleaned.lower():
            return name

    # 3) Substring match: topic name appears somewhere in the reply
    # Sort by name length descending so longer names match first,
    # avoiding "AI" matching when "AI安全" was meant.
    reply_lower = reply.lower()
    for name in sorted(names, key=lambda n: len(n), reverse=True):
        if name.lower() in reply_lower:
            return name

    return UNCATEGORIZED
