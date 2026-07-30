from datetime import datetime, timezone

from app.llm.providers.mock import MockProvider
from app.models import Article
from app.pipeline.relevance import filter_relevant


def _articles(n):
    return [
        Article(title=f"title {i}", content_md=f"body {i}",
                url=f"https://x.com/{i}", source_name="X", source_type="rss",
                published_at=None, images=[], raw_summary=None,
                fetched_at=datetime.now(timezone.utc))
        for i in range(n)
    ]


def test_filter_relevant_keeps_only_returned_indices():
    arts = _articles(3)
    # Model says keep indices 0 and 2 → article 1 dropped.
    provider = MockProvider(responses=["[0, 2]"])
    kept = filter_relevant(arts, provider, enabled=True)
    assert kept == [arts[0], arts[2]]


def test_filter_relevant_fail_open_on_garbage():
    arts = _articles(3)
    provider = MockProvider(responses=["对不起，我无法完成该请求"])
    kept = filter_relevant(arts, provider, enabled=True)
    assert kept == arts


def test_filter_relevant_fail_open_on_error():
    arts = _articles(3)

    class ErrorProvider:
        def chat(self, messages, **opts):
            raise RuntimeError("timeout")

    kept = filter_relevant(arts, ErrorProvider(), enabled=True)
    assert kept == arts


def test_filter_relevant_disabled_passthrough():
    arts = _articles(3)

    class BoomProvider:
        def chat(self, messages, **opts):
            raise AssertionError("should not be called when disabled")

    kept = filter_relevant(arts, BoomProvider(), enabled=False)
    assert kept == arts


def test_filter_relevant_empty_array_drops_all():
    arts = _articles(2)
    provider = MockProvider(responses=["[]"])
    kept = filter_relevant(arts, provider, enabled=True)
    assert kept == []


def test_filter_relevant_batches_across_chunks():
    # 20 articles → 3 chunks of 8/8/4. Each chunk keeps its local index 0 only.
    arts = _articles(20)
    provider = MockProvider(responses=["[0]", "[0]", "[0]"])
    kept = filter_relevant(arts, provider, enabled=True)
    assert kept == [arts[0], arts[8], arts[16]]


def test_informational_scope_keeps_guidance_and_explainers_in_prompt():
    arts = _articles(1)

    class CaptureProvider(MockProvider):
        def __init__(self):
            super().__init__(responses=["[0]"])
            self.prompt = ""

        def chat(self, messages, **opts):
            self.prompt = messages[-1].content
            return super().chat(messages, **opts)

    provider = CaptureProvider()
    assert filter_relevant(
        arts, provider, enabled=True,
        content_scope="informational") == arts
    assert "科普" in provider.prompt
    assert "医生/专家建议" in provider.prompt
