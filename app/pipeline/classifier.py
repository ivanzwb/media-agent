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
    for name in names:
        if name.lower() == reply.lower():
            return name
    return UNCATEGORIZED
