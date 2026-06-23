from app.sources.scraper import discover_links, scrape_single

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
