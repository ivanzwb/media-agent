from datetime import datetime, timezone

from app.models import Article
from app.feeds import Topic
from app.llm.providers.mock import MockProvider
from app.pipeline.classifier import classify


def art(title, body):
    return Article(title=title, content_md=body, url="https://x.com/a",
                   source_name="s", source_type="rss", published_at=None,
                   images=[], raw_summary=None,
                   fetched_at=datetime.now(timezone.utc))


TOPICS = [Topic(name="AI", keywords=["LLM", "GPT"]),
          Topic(name="物理AI", keywords=["robotics", "world model"])]


def test_classify_by_keyword():
    a = art("New GPT model", "A new LLM was released")
    assert classify(a, TOPICS, MockProvider()) == "AI"


def test_classify_physical_ai_keyword():
    a = art("Robot news", "advances in robotics and world model")
    assert classify(a, TOPICS, MockProvider()) == "物理AI"


def test_classify_falls_back_to_llm():
    a = art("Ambiguous title", "no obvious keyword present here")
    provider = MockProvider(responses=["AI"])
    assert classify(a, TOPICS, provider) == "AI"


def test_classify_llm_invalid_returns_uncategorized():
    a = art("Ambiguous", "nothing matches")
    provider = MockProvider(responses=["not-a-real-topic"])
    assert classify(a, TOPICS, provider) == "uncategorized"
