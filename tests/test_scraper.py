from app.sources.scraper import (
    discover_links, scrape_single, scrape_list,
    _is_non_article, _hrefs, _pagination_links)

LIST_HTML = """
<html><body>
<a href="/posts/a">Post A</a>
<a href="/posts/b">Post B</a>
<a href="https://other.com/x">External</a>
<a href="/about">About</a>
</body></html>
"""

ARTICLE_HTML = """
<html><head><title>Post A</title></head><body>
<article><h1>Post A Title</h1><p>Real content about AI here.</p>
<img src="https://example.com/a.png"/></article>
</body></html>
"""


def test_discover_links_filters_same_domain_and_pattern():
    links = discover_links(LIST_HTML, base_url="https://blog.example.com/",
                           include_pattern="/posts/")
    assert "https://blog.example.com/posts/a" in links
    assert "https://blog.example.com/posts/b" in links
    assert all("other.com" not in l for l in links)
    assert all("/about" not in l for l in links)


NAV_HTML = """
<html><body>
<a href="/news/gpt-5-mystery">GPT-5 mystery</a>
<a href="/news/another-story">Another</a>
<a href="/policy">Policy</a>
<a href="/about">About</a>
<a href="/careers">Careers</a>
<a href="/pricing">Pricing</a>
<a href="/files/report.pdf">Report</a>
</body></html>
"""


def test_discover_links_skips_non_article_pages():
    # No include_pattern: built-in denylist should still drop junk pages.
    links = discover_links(NAV_HTML, base_url="https://www.anthropic.com/")
    assert "https://www.anthropic.com/news/gpt-5-mystery" in links
    assert "https://www.anthropic.com/news/another-story" in links
    for junk in ("/policy", "/about", "/careers", "/pricing", "report.pdf"):
        assert all(junk not in l for l in links), junk


def test_discover_links_exclude_pattern():
    links = discover_links(NAV_HTML, base_url="https://www.anthropic.com/",
                           exclude_pattern="another")
    assert "https://www.anthropic.com/news/gpt-5-mystery" in links
    assert all("another" not in l for l in links)


def test_hrefs_handles_quote_styles():
    html = ("<a href=\"/d\">d</a>"
            "<a href='/s'>s</a>"
            "<a HREF=/u>u</a>"
            "<A\n  href = \"/nl\" >nl</a>")
    hrefs = _hrefs(html)
    assert "/d" in hrefs and "/s" in hrefs and "/u" in hrefs and "/nl" in hrefs


def test_discover_links_finds_single_and_unquoted(monkeypatch):
    html = ("<a href='/posts/x'>x</a>"
            "<a HREF=/posts/y>y</a>")
    links = discover_links(html, base_url="https://b.com/", include_pattern="/posts/")
    assert "https://b.com/posts/x" in links
    assert "https://b.com/posts/y" in links


def test_is_non_article_only_blocks_last_segment():
    # section landing pages (in the denylist) blocked
    assert _is_non_article("https://x.com/about")
    assert _is_non_article("https://x.com/careers")
    assert _is_non_article("https://x.com/events")       # denylisted section
    assert _is_non_article("https://x.com/files/a.pdf")  # asset
    # article-container sections (news/blog/research) are NOT denylisted
    assert not _is_non_article("https://x.com/news")
    # deeper article slugs are kept (the bug in issue #7)
    assert not _is_non_article("https://x.com/news/gpt-5")
    assert not _is_non_article("https://x.com/events/ai-summit-2026")
    assert not _is_non_article("https://x.com/blog/download-whitepaper-ai")
    assert not _is_non_article("https://x.com/research/some-paper")


def test_pagination_links_detected():
    html = ("<a href='/research?page=2'>2</a>"
            "<a href='/research/page/3'>3</a>"
            "<a href='/research/some-article'>art</a>")
    pages = _pagination_links(html, "https://x.com/research")
    assert "https://x.com/research?page=2" in pages
    assert "https://x.com/research/page/3" in pages
    assert all("some-article" not in p for p in pages)


def test_scrape_list_follows_pagination(monkeypatch):
    pages = {
        "https://x.com/research": (
            "<a href='/research/a'>a</a><a href='/research?page=2'>next</a>"),
        "https://x.com/research?page=2": "<a href='/research/b'>b</a>",
    }
    monkeypatch.setattr("app.sources.scraper._fetch_html",
                        lambda url, **k: pages.get(url, ""))

    from app.models import Article
    from datetime import datetime, timezone

    def fake_single(link, source_name, **k):
        return Article(title=link, content_md="x", url=link,
                       source_name=source_name, source_type="scrape",
                       published_at=None, images=[], raw_summary=None,
                       fetched_at=datetime.now(timezone.utc))
    monkeypatch.setattr("app.sources.scraper.scrape_single", fake_single)

    arts = scrape_list("https://x.com/research", "HAI",
                       max_pages=2, delay=0)
    urls = {a.url for a in arts}
    assert "https://x.com/research/a" in urls   # page 1
    assert "https://x.com/research/b" in urls   # page 2


def test_scrape_single_builds_article(monkeypatch):
    def fake_get(url, **kwargs):
        class R:
            text = ARTICLE_HTML

            def raise_for_status(self):
                ...
        return R()
    monkeypatch.setattr("app.sources.scraper.httpx.get", fake_get)
    art = scrape_single("https://blog.example.com/posts/a", source_name="Blog")
    assert art is not None
    assert "Post A" in art.title
    assert "AI" in art.content_md
    assert art.source_type == "scrape"
    assert any("a.png" in i for i in art.images)
