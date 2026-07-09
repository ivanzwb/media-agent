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
<article><h1>Post A Title</h1>
<p>Real content about AI here. This is a longer article with multiple paragraphs
so that trafilatura extracts enough text to pass the 300-char article content
check. Artificial intelligence continues to reshape our world in profound and
unexpected ways. Researchers are pushing the boundaries of what machines can
learn and accomplish. From natural language processing to computer vision,
the applications are vast and growing.</p>
<p>Another paragraph is needed to ensure we have multiple text lines after
extraction. This second paragraph adds more depth to the article body and
makes it look like a real article rather than a thin-content page.</p>
<p>A third paragraph ensures we pass the minimum-text-lines check as well.
The article content validation function requires at least three non-link
text lines to consider a page as a genuine article.</p>
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


def test_is_non_article_three_tier():
    """Verify the URL rejection tiers:

    Tier 1 — asset extensions.
    Tier 2 — ANY segment in _HARD_NON_ARTICLE (legal/auth) — any depth.
    Tier 3 — ANY segment in _SOFT_NON_ARTICLE (org sections) — only depth ≤2.
    Tier 4 — terminal segment in _NON_ARTICLE_TERMINAL AND depth ≤2.
    """
    # ── Tier 1: asset files ─────────────────────────────────────────
    assert _is_non_article("https://x.com/files/a.pdf")

    # ── Tier 2/3: shallow non-article sections → reject ─────────────
    assert _is_non_article("https://x.com/about")            # direct (soft, depth 1)
    assert _is_non_article("https://x.com/careers")          # direct (soft, depth 1)
    assert _is_non_article("https://x.com/about-us/team")    # soft, depth 2
    assert _is_non_article("https://x.com/awards/2025")       # soft, depth 2
    assert _is_non_article(
        "https://x.com/about-us/achievement-awards")          # soft, depth 2

    # ── Tier 4: container landing pages (terminal + shallow) ───────
    assert _is_non_article("https://x.com/news")             # depth 1
    assert _is_non_article("https://x.com/events")            # depth 1
    assert _is_non_article("https://x.com/quantum/blog")      # depth 2

    # ── Deeper article slugs are always kept ─────────────────────────
    assert not _is_non_article("https://x.com/news/gpt-5")               # depth 2, terminal not blocked
    assert not _is_non_article("https://x.com/events/ai-summit-2026")    # depth 2, terminal not blocked
    assert not _is_non_article("https://x.com/blog/download-whitepaper-ai")  # depth 2, terminal not blocked
    assert not _is_non_article("https://x.com/research/some-paper")      # depth 2, terminal not blocked
    assert not _is_non_article("https://x.com/quantum/blog/some-post")   # depth 3
    assert not _is_non_article("https://x.com/news-events/3gpp-news/release-18")  # depth 3


def test_is_non_article_soft_denylist_is_depth_aware():
    """Owner P0: an org section in a DEEP path must NOT drop a real article."""
    # soft segment ("company", "products") deep in the path → kept
    assert not _is_non_article("https://x.com/company/blog/announcing-gpt-5")
    assert not _is_non_article("https://x.com/products/2024/launch-story")
    assert not _is_non_article("https://x.com/about/engineering/scaling-our-db")
    # but still blocked when shallow
    assert _is_non_article("https://x.com/company")
    assert _is_non_article("https://x.com/company/overview")


def test_is_non_article_hard_denylist_any_depth():
    """Legal/auth/account pages are never articles, even deeply nested."""
    assert _is_non_article("https://x.com/legal/terms-of-use")
    assert _is_non_article("https://x.com/help/legal/privacy-policy")
    assert _is_non_article("https://x.com/a/b/c/login")
    assert _is_non_article("https://x.com/account/settings/password")
    assert _is_non_article("https://x.com/x/y/cookie-policy")


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


def test_scrape_list_falls_back_to_sitemap(monkeypatch):
    # List page has NO article links (e.g. JS-rendered) -> fallback to sitemap.
    pages = {
        "https://e.com/blog": "<html><body><nav>no article links</nav></body></html>",
        "https://e.com/robots.txt": "Sitemap: https://e.com/sitemap.xml\n",
        "https://e.com/sitemap.xml": (
            "<urlset>"
            "<url><loc>https://e.com/posts/a</loc></url>"
            "<url><loc>https://e.com/posts/b</loc></url>"
            "</urlset>"),
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

    arts = scrape_list("https://e.com/blog", "E", max_pages=1, delay=0)
    urls = {a.url for a in arts}
    assert "https://e.com/posts/a" in urls
    assert "https://e.com/posts/b" in urls


def test_fetch_html_retries_transient_then_succeeds(monkeypatch):
    import httpx
    from app.sources import scraper
    monkeypatch.setattr(scraper.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    class R:
        text = "OK"

        def raise_for_status(self):
            ...

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.TimeoutException("boom")
        return R()
    monkeypatch.setattr(scraper.httpx, "get", flaky)
    assert scraper._fetch_html("https://x.com/a", retries=3) == "OK"
    assert calls["n"] == 3   # retried twice, succeeded on 3rd


def test_fetch_html_no_retry_on_4xx(monkeypatch):
    import pytest
    import httpx
    from app.sources import scraper
    monkeypatch.setattr(scraper.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def get404(*a, **k):
        calls["n"] += 1
        req = httpx.Request("GET", "https://x.com/b")
        resp = httpx.Response(404, request=req)
        raise httpx.HTTPStatusError("404", request=req, response=resp)
    monkeypatch.setattr(scraper.httpx, "get", get404)
    with pytest.raises(httpx.HTTPStatusError):
        scraper._fetch_html("https://x.com/b", retries=3)
    assert calls["n"] == 1   # 4xx is permanent — no retries


def test_fetch_html_rotates_ua_on_retry(monkeypatch):
    """Each retry attempt uses a different User-Agent."""
    import httpx
    from app.sources import scraper
    monkeypatch.setattr(scraper.time, "sleep", lambda *_: None)
    uas = []

    def record_ua(*a, **k):
        uas.append(k.get("headers", {}).get("User-Agent", ""))
        raise httpx.TimeoutException("boom")
    monkeypatch.setattr(scraper.httpx, "get", record_ua)

    try:
        scraper._fetch_html("https://x.com/c", retries=3)
    except httpx.TimeoutException:
        pass

    # Default retries=3 → 3 attempts
    assert len(uas) == 3
    # At least one UA should differ from the first
    assert len(set(uas)) >= 2


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


# ═══════════════════════════════════════════════════════════════════
# _is_article_content
# ═══════════════════════════════════════════════════════════════════

def test_article_content_rejects_thin_body():
    from app.sources.scraper import _is_article_content
    body = "short text"
    assert not _is_article_content({"title": "Title", "content_md": body})


def test_article_content_rejects_too_few_text_lines():
    from app.sources.scraper import _is_article_content
    # Only 1 text line; everything else is bare URLs or images.
    body = (
        "https://example.com/link1\n"
        "https://example.com/link2\n"
        "https://example.com/link3\n"
        "![img](/a.png)\n"
        "![img](/b.png)\n"
        "Real text line here\n"
    )
    assert not _is_article_content({"title": "Title", "content_md": body})


def test_article_content_rejects_high_link_density():
    """Listing / year-index pages where >40% of non-whitespace chars are in
    [text](url) markdown links."""
    from app.sources.scraper import _is_article_content
    body = (
        "[News 2024: AI Breakthrough](https://nims.go.jp/eng/news/2024/ai.html)\n"
        "[Research Update Jan](https://nims.go.jp/eng/news/2024/jan.html)\n"
        "[Research Update Feb](https://nims.go.jp/eng/news/2024/feb.html)\n"
        "[Research Update Mar](https://nims.go.jp/eng/news/2024/mar.html)\n"
        "[Research Update Apr](https://nims.go.jp/eng/news/2024/apr.html)\n"
        "[Research Update May](https://nims.go.jp/eng/news/2024/may.html)\n"
        "[Research Update Jun](https://nims.go.jp/eng/news/2024/jun.html)\n"
        "[Research Update Jul](https://nims.go.jp/eng/news/2024/jul.html)\n"
    )
    assert not _is_article_content({"title": "News 2024", "content_md": body})


def test_article_content_rejects_404_titles():
    from app.sources.scraper import _is_article_content
    body = "Some page content that is long enough to pass the 300-char threshold. " * 10
    for title in ("404", "Page Not Found", "Not Found"):
        assert not _is_article_content({"title": title, "content_md": body})


def test_article_content_rejects_broader_404_wording():
    """Titles combining "404" with "not found" are rejected (e.g. "404 Not Found – Emotiv")."""
    from app.sources.scraper import _is_article_content
    body = "Some page content that is long enough to pass the 300-char threshold. " * 10
    for title in ("404 Not Found – Emotiv", "404 – Page Not Found",
                  "Error 404: Not Found"):
        assert not _is_article_content({"title": title, "content_md": body}), title


def test_article_content_rejects_listing_titles():
    """Newsletter / Archive / Blog Archive / Press Release titles are listing pages."""
    from app.sources.scraper import _is_article_content
    body = "Some page content that is long enough to pass the 300-char threshold. " * 10
    for title in ("Newsletter", "Newsletter – Jan 2026",
                  "Archive", "Blog Archive",
                  "Press Release", "Press Releases",
                  "News & Events", "News Events",
                  "Article Archive"):
        assert not _is_article_content({"title": title, "content_md": body}), title


def test_article_content_keeps_news_in_title():
    """A real article whose title merely CONTAINS "News" should NOT be rejected."""
    from app.sources.scraper import _is_article_content
    body = """## Latest AI News

This is a real article about the latest developments in artificial intelligence.
The title starts with "Latest AI News" not the bare word "News" so it should
be treated as a genuine article. The text is long enough to pass all checks and
contains actual prose rather than a list of links. Machine learning continues to
advance at a rapid pace, with new breakthroughs every month. Researchers are
finding novel applications for deep learning in healthcare, robotics, and more.
These developments promise to transform industries across the board."""
    assert _is_article_content({"title": "Latest AI News", "content_md": body})


def test_article_content_accepts_valid_article():
    from app.sources.scraper import _is_article_content
    body = """## Introduction

This is the first paragraph of a real article. It has multiple sentences with
substantial content that describes the topic in detail. Artificial intelligence
continues to reshape our world in profound and unexpected ways. Researchers are
pushing the boundaries of what machines can learn and accomplish.

## Background

The second paragraph provides historical context and necessary background
information to help readers understand the significance of these developments.
Natural language processing has seen remarkable advances in recent years.

## Key Findings

A third paragraph that presents the main findings or arguments of the article.
This ensures we have plenty of text content to pass the minimum length check.
The three text-line minimum is easily satisfied by this real article content.

## Conclusion

The final paragraph wraps up the discussion and offers concluding thoughts on
the topic. Future research directions and open questions are also discussed."""

    assert _is_article_content({"title": "AI Developments", "content_md": body})


# ═══════════════════════════════════════════════════════════════════
# _is_non_article  —  index.* tier 4b
# ═══════════════════════════════════════════════════════════════════

def test_is_non_article_index_pages():
    """Index pages (index.html, index.php, index.aspx) are rejected at any
    depth — they are always listing / container pages."""
    assert _is_non_article(
        "https://mccormick.northwestern.edu/materials-science/"
        "news-events/events/index.php")

    # All the major extensions
    assert _is_non_article("https://x.com/news/index.html")
    assert _is_non_article("https://x.com/news/index.php")
    assert _is_non_article("https://x.com/news/index.aspx")
    assert _is_non_article("https://x.com/index.htm")

    # But a real article slug like "indexical-philosophy" is NOT blocked
    # because it doesn't start with "index."
    assert not _is_non_article("https://x.com/blog/indexical-philosophy")


def test_is_non_article_html_extension_stripping():
    """Hard-denylist words with .html/.php suffix are also rejected.

    e.g. /materials-science/news-events/newsletter.html has segment
    "newsletter.html" which is NOT directly in _HARD_NON_ARTICLE, but
    after stripping the .html → "newsletter" which IS.
    """
    assert _is_non_article(
        "https://mccormick.northwestern.edu/materials-science/"
        "news-events/newsletter.html")
    assert _is_non_article("https://x.com/legal/terms.html")
    assert _is_non_article("https://x.com/cookie-policy.aspx")
    assert _is_non_article("https://x.com/privacy/index.html")


# ── _fetch_html: plain vs rendered comparison ────────────────────────────

from unittest.mock import patch

@patch("app.sources.scraper._render_with_playwright")
@patch("app.sources.scraper.extract_from_html")
def test_fetch_html_chooses_plain_when_richer(mock_extract, mock_render):
    """When plain HTML yields significantly more content, use it."""
    mock_render.return_value = "<html><body>rendered</body></html>"
    # First call: rendered content (short)
    # Second call: plain content (long)
    mock_extract.side_effect = [
        {"content_md": "short content"},
        {"content_md": "much longer content " + "x" * 100},
    ]
    from app.sources.scraper import _fetch_html
    # Monkey-patch httpx.get to return plain HTML
    with patch("app.sources.scraper.httpx.get") as mock_get:
        mock_get.return_value.text = "<html><body>plain html</body></html>"
        mock_get.return_value.raise_for_status = lambda: None
        result = _fetch_html("https://example.com", render_js=True, timeout=10)
        # Longer content should win → plain HTML returned
        assert "plain html" in result


@patch("app.sources.scraper._render_with_playwright")
@patch("app.sources.scraper.extract_from_html")
def test_fetch_html_keeps_rendered_when_richer(mock_extract, mock_render):
    """When rendered HTML is richer (or similar), keep it."""
    mock_render.return_value = "<html><body>rendered</body></html>"
    # Both are similar — rendered should be kept
    mock_extract.side_effect = [
        {"content_md": "long rendered content " + "x" * 100},
        {"content_md": "short plain"},
    ]
    from app.sources.scraper import _fetch_html
    with patch("app.sources.scraper.httpx.get") as mock_get:
        mock_get.return_value.text = "<html><body>plain html</body></html>"
        mock_get.return_value.raise_for_status = lambda: None
        result = _fetch_html("https://example.com", render_js=True, timeout=10)
        # Rendered should win → rendered HTML returned
        assert "rendered" in result


@patch("app.sources.scraper._render_with_playwright")
def test_fetch_html_falls_back_when_playwright_fails(mock_render):
    """When Playwright returns None, fall back to plain httpx."""
    mock_render.return_value = None
    from app.sources.scraper import _fetch_html
    with patch("app.sources.scraper.httpx.get") as mock_get:
        mock_get.return_value.text = "<html><body>plain fallback</body></html>"
        mock_get.return_value.raise_for_status = lambda: None
        result = _fetch_html("https://example.com", render_js=True, timeout=10)
        assert "plain fallback" in result


@patch("app.sources.scraper._render_with_playwright")
@patch("app.sources.scraper.extract_from_html")
def test_fetch_html_keeps_rendered_when_plain_fetch_fails(mock_extract, mock_render):
    """When plain HTTP fetch fails, keep the rendered HTML."""
    mock_render.return_value = "<html><body>rendered only</body></html>"
    # Only called for rendered (plain fails before extraction)
    mock_extract.return_value = {"content_md": "rendered content " + "x" * 100}
    from app.sources.scraper import _fetch_html
    with patch("app.sources.scraper.httpx.get",
               side_effect=Exception("Connection refused")):
        result = _fetch_html("https://example.com", render_js=True, timeout=10)
        assert "rendered only" in result


@patch("app.sources.scraper._render_with_playwright")
@patch("app.sources.scraper.extract_from_html")
def test_fetch_html_keeps_rendered_when_similar_length(mock_extract, mock_render):
    """When plain and rendered content are similar (< 1.2x), keep rendered."""
    mock_render.return_value = "<html><body>rendered</body></html>"
    # 100 vs 110: ratio 1.1 < 1.2 → keep rendered
    mock_extract.side_effect = [
        {"content_md": "rendered content " + "x" * 100},
        {"content_md": "plain content "   + "x" * 110},
    ]
    from app.sources.scraper import _fetch_html
    with patch("app.sources.scraper.httpx.get") as mock_get:
        mock_get.return_value.text = "<html><body>plain</body></html>"
        mock_get.return_value.raise_for_status = lambda: None
        result = _fetch_html("https://example.com", render_js=True, timeout=10)
        assert "rendered" in result


@patch("app.sources.scraper._render_with_playwright")
def test_fetch_html_no_render_js_returns_plain_directly(mock_render):
    """When render_js=False, skip Playwright entirely, just httpx."""
    from app.sources.scraper import _fetch_html
    with patch("app.sources.scraper.httpx.get") as mock_get:
        mock_get.return_value.text = "<html><body>plain only</body></html>"
        mock_get.return_value.raise_for_status = lambda: None
        result = _fetch_html("https://example.com", render_js=False, timeout=10)
        mock_render.assert_not_called()
        assert "plain only" in result
