import httpx

from app.feeds import check_url_connectivity, load_feeds, ensure_feeds_file, FeedsConfig

YAML = """
topics:
  - name: AI
    keywords: [AI, LLM]
  - name: 物理AI
    keywords: [robotics, world model]
sources:
  - name: OpenAI Blog
    type: rss
    url: https://openai.com/blog/rss.xml
    topics: [AI]
  - name: DeepMind Blog
    type: scrape
    mode: list
    url: https://deepmind.google/discover/blog/
    include_pattern: /discover/blog/
    topics: [AI, 物理AI]
"""


# ── check_url_connectivity ─────────────────────────────────────────


class _MockResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=self)


def _mock_httpx(method: str, url: str, **kw) -> _MockResponse:
    if "reachable" in url:
        return _MockResponse(200)
    if "notfound" in url:
        return _MockResponse(404)
    if "servererr" in url:
        return _MockResponse(500)
    if "head-fail" in url and method == "HEAD":
        raise httpx.ConnectError("HEAD failed")
    if "head-fail" in url and method == "GET":
        return _MockResponse(200)
    raise httpx.ConnectError("totally unreachable")


def test_check_url_connectivity_reachable(monkeypatch):
    monkeypatch.setattr("app.feeds.httpx.request", _mock_httpx)
    assert check_url_connectivity("https://reachable.example.com") is True


def test_check_url_connectivity_not_found_returns_false(monkeypatch):
    monkeypatch.setattr("app.feeds.httpx.request", _mock_httpx)
    assert check_url_connectivity("https://notfound.example.com") is False


def test_check_url_connectivity_server_error_returns_false(monkeypatch):
    monkeypatch.setattr("app.feeds.httpx.request", _mock_httpx)
    assert check_url_connectivity("https://servererr.example.com") is False


def test_check_url_connectivity_head_fails_get_succeeds(monkeypatch):
    """HEAD fails (connection error) but GET succeeds → reachable."""
    monkeypatch.setattr("app.feeds.httpx.request", _mock_httpx)
    assert check_url_connectivity("https://head-fail.example.com") is True


def test_check_url_connectivity_totally_unreachable(monkeypatch):
    monkeypatch.setattr("app.feeds.httpx.request", _mock_httpx)
    assert check_url_connectivity("https://dead.example.com") is False


def test_check_url_connectivity_passes_proxy(monkeypatch):
    captured: list[str | None] = [None]

    def _check_proxy(method, url, **kw):
        captured[0] = kw.get("proxy")
        return _MockResponse(200)

    monkeypatch.setattr("app.feeds.httpx.request", _check_proxy)
    assert check_url_connectivity("https://reachable.example.com",
                                  proxy="http://proxy:8080") is True
    assert captured[0] == "http://proxy:8080"


# ── FeedsConfig load tests ─────────────────────────────────────────


def test_load_feeds_parses_topics_and_sources(tmp_path):
    p = tmp_path / "feeds.yaml"
    p.write_text(YAML, encoding="utf-8")
    cfg = load_feeds(p)
    assert isinstance(cfg, FeedsConfig)
    assert {t.name for t in cfg.topics} == {"AI", "物理AI"}
    assert cfg.topics[0].keywords == ["AI", "LLM"]
    assert len(cfg.sources) == 2
    assert cfg.sources[1].mode == "list"
    assert cfg.sources[1].include_pattern == "/discover/blog/"


def test_load_feeds_defaults_mode_single_for_scrape(tmp_path):
    p = tmp_path / "feeds.yaml"
    p.write_text("""
topics: [{name: AI, keywords: [AI]}]
sources: [{name: S, type: scrape, url: https://x.com/a, topics: [AI]}]
""", encoding="utf-8")
    cfg = load_feeds(p)
    assert cfg.sources[0].mode == "single"


def test_load_feeds_missing_file_returns_empty(tmp_path):
    # Fresh/packaged install has no feeds.yaml — must not crash.
    cfg = load_feeds(tmp_path / "nope.yaml")
    assert isinstance(cfg, FeedsConfig)
    assert cfg.topics == [] and cfg.sources == []


def test_ensure_feeds_file_creates_empty_template_when_missing(tmp_path):
    p = tmp_path / "sub" / "feeds.yaml"
    assert not p.exists()
    ensure_feeds_file(p)
    assert p.exists()
    # The created file is a valid, empty template.
    cfg = load_feeds(p)
    assert cfg.topics == [] and cfg.sources == []


def test_ensure_feeds_file_does_not_overwrite_existing(tmp_path):
    p = tmp_path / "feeds.yaml"
    p.write_text(YAML, encoding="utf-8")
    ensure_feeds_file(p)
    # Existing content is preserved (only 2 sources from YAML, not defaults).
    cfg = load_feeds(p)
    assert len(cfg.sources) == 2
