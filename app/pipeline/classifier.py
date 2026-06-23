from __future__ import annotations

from app.feeds import Topic
from app.llm.base import LLMProvider, Message
from app.models import Article

UNCATEGORIZED = "uncategorized"


def _keyword_match(article: Article, topics: list[Topic]) -> str | None:
    haystack = f"{article.title}\n{article.content_md}".lower()
    for topic in topics:
        for kw in topic.keywords:
            if kw.lower() in haystack:
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
