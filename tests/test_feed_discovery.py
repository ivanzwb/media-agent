from app.sources.feed_discovery import (
    find_feed_urls, feed_entry_links, sitemap_urls)


def test_find_feed_urls_detects_rss_and_atom():
    html = (
        '<head>'
        '<link rel="stylesheet" href="/x.css">'
        '<link rel="alternate" type="application/rss+xml" href="/feed.xml">'
        '<link rel="alternate" type="application/atom+xml" href="https://e.com/atom">'
        '<link rel="icon" href="/favicon.ico">'
        '</head>'
    )
    feeds = find_feed_urls(html, base_url="https://e.com/blog")
    assert "https://e.com/feed.xml" in feeds
    assert "https://e.com/atom" in feeds
    assert all(".css" not in f and "favicon" not in f for f in feeds)


def test_feed_entry_links_parses_rss():
    xml = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <title>Blog</title><link>https://e.com/</link>
      <item><title>A</title><link>https://e.com/posts/a</link></item>
      <item><title>B</title><link>https://e.com/posts/b</link></item>
    </channel></rss>"""
    links = feed_entry_links(xml)
    assert links == ["https://e.com/posts/a", "https://e.com/posts/b"]


def test_sitemap_urls_follows_index_and_filters():
    pages = {
        "https://e.com/robots.txt": "User-agent: *\nSitemap: https://e.com/sitemap_index.xml\n",
        "https://e.com/sitemap_index.xml": (
            '<?xml version="1.0"?><sitemapindex>'
            '<sitemap><loc>https://e.com/sm-posts.xml</loc></sitemap>'
            "</sitemapindex>"),
        "https://e.com/sm-posts.xml": (
            '<?xml version="1.0"?><urlset>'
            "<url><loc>https://e.com/posts/a</loc></url>"
            "<url><loc>https://e.com/about</loc></url>"
            "<url><loc>https://e.com/posts/b</loc></url>"
            "</urlset>"),
    }

    def fetch(url):
        return pages[url]   # KeyError == fetch failure (caught by caller)

    urls = sitemap_urls("https://e.com/blog", fetch, include_pattern="/posts/")
    assert "https://e.com/posts/a" in urls
    assert "https://e.com/posts/b" in urls
    assert "https://e.com/about" not in urls   # filtered by include_pattern


def test_sitemap_urls_handles_missing_gracefully():
    def fetch(url):
        raise RuntimeError("404")
    assert sitemap_urls("https://e.com/", fetch) == []
