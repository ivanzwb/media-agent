from app.discovery import (find_feed_links, discover_from_url, search_web,
                           discover_from_keyword, probe_feed_paths,
                           looks_like_feed)

FEED_XML = ('<?xml version="1.0"?><rss version="2.0"><channel>'
            '<title>Acme</title></channel></rss>')

PAGE_WITH_FEED = """
<html><head>
<title>Acme AI Blog</title>
<link rel="alternate" type="application/rss+xml" href="/feed.xml"/>
<link rel="stylesheet" href="/style.css"/>
</head><body>hi</body></html>
"""

PAGE_NO_FEED = """
<html><head><title>No Feed Site</title></head>
<body><a href="/post/1">p1</a></body></html>
"""

DDG_HTML = """
<div><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.com%2Fblog&rut=x">A</a></div>
<div><a class="result__a" href="https://b.com/news">B</a></div>
"""


def test_find_feed_links_absolute():
    links = find_feed_links(PAGE_WITH_FEED, "https://acme.com/")
    assert links == ["https://acme.com/feed.xml"]


def test_discover_from_url_rss():
    out = discover_from_url("https://acme.com/", topics=["AI"],
                            fetch=lambda u: PAGE_WITH_FEED)
    assert len(out) == 1
    assert out[0].type == "rss"
    assert out[0].url == "https://acme.com/feed.xml"
    assert out[0].name == "Acme AI Blog"
    assert out[0].topics == ["AI"]


def test_discover_from_url_fallback_scrape():
    out = discover_from_url("https://nofeed.com/", topics=["AI"],
                            fetch=lambda u: PAGE_NO_FEED)
    assert len(out) == 1
    assert out[0].type == "scrape"
    assert out[0].mode == "list"
    assert out[0].url == "https://nofeed.com/"


def test_discover_from_url_fetch_error_returns_empty():
    def boom(u):
        raise RuntimeError("net down")
    assert discover_from_url("https://x.com", fetch=boom) == []


def test_looks_like_feed():
    assert looks_like_feed(FEED_XML)
    assert looks_like_feed('<feed xmlns="http://www.w3.org/2005/Atom"></feed>')
    assert not looks_like_feed(PAGE_NO_FEED)


def test_probe_feed_paths_finds_conventional_feed():
    # No autodiscovery <link>, but a feed lives at the conventional /feed path.
    def fake_fetch(url):
        if url == "https://acme.com/feed":
            return FEED_XML
        return PAGE_NO_FEED

    assert (probe_feed_paths("https://acme.com/blog", fetch=fake_fetch)
            == "https://acme.com/feed")


def test_probe_feed_paths_returns_none_when_no_feed():
    assert probe_feed_paths("https://acme.com/", fetch=lambda u: PAGE_NO_FEED) is None


def test_search_web_parses_and_decodes():
    urls = search_web("ai news", fetch=lambda u: DDG_HTML)
    assert "https://a.com/blog" in urls
    assert "https://b.com/news" in urls


def test_discover_from_keyword_combines(monkeypatch):
    pages = {
        "https://a.com/blog": PAGE_WITH_FEED,
        "https://b.com/news": PAGE_NO_FEED,
    }

    def fake_fetch(url):
        if "duckduckgo" in url:
            return DDG_HTML
        return pages[url]

    out = discover_from_keyword("ai", topics=["AI"], fetch=fake_fetch)
    types = {s.type for s in out}
    assert "rss" in types and "scrape" in types
    assert all(s.topics == ["AI"] for s in out)
