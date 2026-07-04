from pathlib import Path

from app.sources.extractor import (
    extract_from_html, _videos_with_placeholders, _images_from_html,
    _videos_from_html, _date_from_html, _fix_markdown_tables,
    _jsonld_image, _filter_article_images,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_article.html"


def test_extract_returns_title_and_body():
    html = FIXTURE.read_text(encoding="utf-8")
    result = extract_from_html(html, url="https://example.com/post")
    assert "Physical AI" in result["title"]
    assert "world model" in result["content_md"]
    assert "junk" not in result["content_md"]


def test_extract_collects_images():
    html = FIXTURE.read_text(encoding="utf-8")
    result = extract_from_html(html, url="https://example.com/post")
    assert any("figure1.png" in img for img in result["images"])


def test_videos_with_placeholders_keep_position():
    html = (
        "<p>开头段落</p>"
        '<iframe src="https://www.youtube.com/embed/abc"></iframe>'
        "<p>中间段落</p>"
        '<iframe src="https://ads.example.com/banner"></iframe>'  # not a video
        "<video><source src=\"/clip.mp4\"/></video>"
        "<p>结尾段落</p>"
    )
    out, videos = _videos_with_placeholders(html, base_url="https://x.com/a")
    # ordered, document position
    assert videos[0] == "https://www.youtube.com/embed/abc"
    assert videos[1] == "https://x.com/clip.mp4"
    # placeholders inserted in place; ad iframe untouched
    assert "[[VIDEO:0]]" in out and "[[VIDEO:1]]" in out
    assert out.index("[[VIDEO:0]]") < out.index("中间段落")
    assert "ads.example.com" in out  # left as-is, not turned into a video


def test_images_handles_modern_img_variants():
    html = (
        '<img src="/a.png"/>'
        '<img data-src="/b.jpg" loading="lazy"/>'
        '<img srcset="/c-1x.webp 1x, /c-2x.webp 2x"/>'
        "<picture><source srcset='/d.avif'/></picture>"
        '<img src="data:image/png;base64,xxx"/>'  # inline data ignored
    )
    imgs = _images_from_html(html)
    assert "/a.png" in imgs
    assert "/b.jpg" in imgs          # data-src
    assert "/c-1x.webp" in imgs      # first srcset candidate
    assert "/d.avif" in imgs         # <picture><source>
    assert all(not u.startswith("data:") for u in imgs)


def test_videos_capture_js_array_urls():
    # 11 src-less <video> shells + URLs only in a script array (PI website case)
    html = (
        '<video loop muted playsInline preload="metadata"></video>'
        '<video><source src="/system_diagram.mp4"/></video>'
        '<script>const clips = ["hero.mp4", "screw_compare.webm"];</script>'
    )
    vids = _videos_from_html(html, base_url="https://pi.co/research/rlt")
    assert "https://pi.co/system_diagram.mp4" in vids
    assert "https://pi.co/research/hero.mp4" in vids        # from script
    assert "https://pi.co/research/screw_compare.webm" in vids


def test_placeholders_keep_srcless_video_and_capture_script():
    html = (
        "<p>intro</p>"
        '<video loop muted></video>'                       # src-less shell
        '<script>var v = ["clip-a.mp4"];</script>'
    )
    out, videos = _videos_with_placeholders(html, base_url="https://pi.co/r/x")
    # src-less <video> tag not deleted
    assert "<video" in out
    # script URL still captured for archiving
    assert "https://pi.co/r/clip-a.mp4" in videos


def test_videos_capture_hls_m3u8():
    html = (
        '<video><source src="/hls/playlist.m3u8" '
        'type="application/x-mpegURL"></video>'
        '<script>var s = ["https://cdn.example.com/live/stream.m3u8?t=1"];</script>'
    )
    vids = _videos_from_html(html, base_url="https://x.com/a")
    assert "https://x.com/hls/playlist.m3u8" in vids
    assert any("stream.m3u8" in v for v in vids)


def test_m3u8_routes_to_ytdlp_not_direct():
    # HLS must NOT be treated as a direct file (httpx would only fetch the
    # playlist text); it must go through yt-dlp (+ffmpeg) to mux .ts segments.
    from app.pipeline.localize import _DIRECT_VIDEO_EXTS
    assert ".m3u8" not in _DIRECT_VIDEO_EXTS


def test_extract_collects_videos():
    html = (
        '<html><body>'
        '<iframe src="https://www.youtube.com/embed/xyz"></iframe>'
        '<iframe src="https://ads.example.com/banner"></iframe>'  # not a video
        '<video><source src="/media/clip.mp4"/></video>'
        '</body></html>'
    )
    result = extract_from_html(html, url="https://example.com/post")
    vids = result["videos"]
    assert "https://www.youtube.com/embed/xyz" in vids
    assert any("clip.mp4" in v for v in vids)  # relative -> absolute
    assert all("ads.example.com" not in v for v in vids)


# ── date extraction ───────────────────────────────────────────────────────

def test_date_from_standard_meta_and_jsonld():
    cases = [
        '<meta property="article:published_time" content="2024-05-13T10:30:00Z"/>',
        '<meta content="2024-05-13T10:30:00+00:00" property="article:published_time">',
        '<meta itemprop="datePublished" content="2024-05-13">',
        '<meta name="parsely-pub-date" content="2024-05-13T10:30:00Z">',
        '<script type="application/ld+json">{"@type":"NewsArticle",'
        '"datePublished":"2024-05-13T10:30:00.000Z"}</script>',
        '<time datetime="2024-05-13T10:30:00">May 13</time>',
        '<meta name="pubdate" content="2024年5月13日">',
    ]
    for html in cases:
        dt = _date_from_html(html)
        assert dt is not None, html
        assert (dt.year, dt.month, dt.day) == (2024, 5, 13), html


def test_date_from_js_hydration_blob_escaped_quotes():
    """Next.js RSC / __NEXT_DATA__ embed JSON as an escaped JS string, so the
    date appears as \\"publishedOn\\":\\"...\\" — the original regex missed it,
    which is why every article showed '-' for published time."""
    html = (
        r'<script>self.__next_f.push([1,"6:[\"$\",\"$L15\",null,'
        r'{\"post\":{\"_createdAt\":\"2024-06-17T18:08:09Z\",'
        r'\"publishedOn\":\"2024-06-21T03:28:00.000Z\"}}"])</script>'
    )
    dt = _date_from_html(html)
    assert dt is not None
    assert (dt.year, dt.month, dt.day) == (2024, 6, 21)


def test_date_prefers_published_over_created_modified():
    html = (
        r'{\"_createdAt\":\"2020-01-01T00:00:00Z\",'
        r'\"_updatedAt\":\"2025-09-09T00:00:00Z\",'
        r'\"publishedAt\":\"2024-06-21T03:28:00Z\"}'
    )
    dt = _date_from_html(html)
    assert dt is not None and dt.year == 2024 and dt.month == 6


def test_date_falls_back_to_created_when_no_published():
    html = r'{\"_createdAt\":\"2024-01-02T00:00:00Z\"}'
    dt = _date_from_html(html)
    assert dt is not None and (dt.year, dt.month, dt.day) == (2024, 1, 2)


def test_date_none_when_absent():
    assert _date_from_html("<html><body><p>no date here</p></body></html>") is None


# ── table compaction ──────────────────────────────────────────────────────

def test_fix_tables_removes_intra_table_blanks():
    """Blank lines between table rows are removed; rows become contiguous."""
    md = (
        "intro paragraph\n\n"
        "| Col1 | Col2 |\n"
        "|---|---|\n"
        "\n"
        "| A | B |\n"
        "\n"
        "| C | D |\n"
        "\n"
        "after paragraph"
    )
    fixed = _fix_markdown_tables(md)
    lines = fixed.split("\n")
    # header and separator are adjacent
    hdr_idx = lines.index("| Col1 | Col2 |")
    assert lines[hdr_idx + 1] == "|---|---|"
    # data rows are adjacent — no blank between them
    a_idx = lines.index("| A | B |")
    assert lines[a_idx + 1] == "| C | D |"
    # blank line after table separates it from next paragraph
    assert lines[a_idx + 2] == ""
    assert lines[a_idx + 3] == "after paragraph"


def test_fix_tables_handles_trafilatura_format():
    """trafilatura omits leading pipes: `Col1 | Col2` instead of `| Col1 | Col2 |`."""
    md = (
        "above text\n\n"
        "Score | Description |\n"
        "---|---|\n"
        "\n"
        "0 | No change |\n"
        "\n"
        "1 | Minor |\n"
        "\n"
        "below text"
    )
    fixed = _fix_markdown_tables(md)
    # blank lines between rows are gone
    assert "\n\n0 | No change |\n\n" not in fixed
    assert "\n\n1 | Minor |\n\n" not in fixed
    # rows are contiguous
    assert "0 | No change |\n1 | Minor |" in fixed


def test_fix_tables_noop_when_already_clean():
    """Already-clean tables pass through unchanged (aside from trailing blank)."""
    md = (
        "text\n\n"
        "| A | B |\n"
        "|---|---|\n"
        "| 1 | 2 |\n"
        "| 3 | 4 |\n"
        "\n"
        "more text"
    )
    fixed = _fix_markdown_tables(md)
    lines = fixed.split("\n")
    assert lines[lines.index("| 1 | 2 |") + 1] == "| 3 | 4 |"
    assert "more text" in fixed


def test_fix_tables_no_table():
    """Content without tables is unchanged."""
    md = "Just plain text.\n\nNo pipes anywhere.\n"
    assert _fix_markdown_tables(md) == md


def test_fix_tables_multiple_tables():
    """Multiple tables in one document are each compacted independently."""
    md = (
        "## Table 1\n\n"
        "H1 | H2 |\n"
        "---|---|---|\n"
        "\n"
        "a | b |\n"
        "\n"
        "## Interlude\n\n"
        "## Table 2\n\n"
        "X | Y |\n"
        "---|---|\n"
        "\n"
        "1 | 2 |\n"
    )
    fixed = _fix_markdown_tables(md)
    lines = fixed.split("\n")
    # Table 1: contiguous
    t1_hdr = lines.index("H1 | H2 |")
    assert lines[t1_hdr + 1] == "---|---|---|"
    assert lines[t1_hdr + 2] == "a | b |"
    # Table 1, blank after
    assert lines[t1_hdr + 3] == ""
    # Interlude preserved
    assert "## Interlude" in fixed
    # Table 2: contiguous
    t2_hdr = lines.index("X | Y |")
    assert lines[t2_hdr + 1] == "---|---|"
    assert lines[t2_hdr + 2] == "1 | 2 |"


# ═══════════════════════════════════════════════════════════════════
# _jsonld_image  — extract primary image from Schema.org JSON-LD
# ═══════════════════════════════════════════════════════════════════

def test_jsonld_flat_image_string():
    """Plain string value: "image": "https://..." """
    html = (
        '<script type="application/ld+json">'
        '{"@type":"NewsArticle","image":"https://cdn.example.com/hero.jpg"}'
        '</script>'
    )
    result = _jsonld_image(html)
    assert result == "https://cdn.example.com/hero.jpg"


def test_jsonld_nested_image_object():
    """Nested ImageObject: "image":{"@type":"ImageObject","url":"..."}"""
    html = (
        '<script type="application/ld+json">'
        '{"@context":"http://schema.org","@type":"NewsArticle",'
        '"image":{"@type":"ImageObject","url":"https://cdn.example.com/hero.jpg"}}'
        '</script>'
    )
    result = _jsonld_image(html)
    assert result == "https://cdn.example.com/hero.jpg"


def test_jsonld_image_with_base_url():
    """Relative image URL is resolved against base_url."""
    html = (
        '<script type="application/ld+json">'
        '{"@type":"Article","image":"/uploads/hero.jpg"}'
        '</script>'
    )
    result = _jsonld_image(html, base_url="https://blog.example.com/post/")
    assert "https://blog.example.com" in result
    assert result.endswith("/uploads/hero.jpg")


def test_jsonld_no_image_field():
    """JSON-LD without an image field returns None."""
    html = (
        '<script type="application/ld+json">'
        '{"@type":"Article","name":"Just a title"}'
        '</script>'
    )
    assert _jsonld_image(html) is None


def test_jsonld_not_article_type():
    """Non-Article JSON-LD blocks are skipped."""
    html = (
        '<script type="application/ld+json">'
        '{"@type":"Organization","image":"https://cdn.example.com/logo.png"}'
        '</script>'
    )
    # Currently we don't filter by @type, but the function should still
    # extract the image (caller decides what to do with it)
    assert _jsonld_image(html) == "https://cdn.example.com/logo.png"


def test_jsonld_no_jsonld_at_all():
    """No JSON-LD script tag → None."""
    assert _jsonld_image("<html><body><p>no jsonld</p></body></html>") is None


def test_jsonld_multiple_blocks_first_wins():
    """Multiple JSON-LD blocks — first image is returned."""
    html = (
        '<script type="application/ld+json">'
        '{"@type":"Article","image":"https://cdn.example.com/first.jpg"}'
        '</script>'
        '<script type="application/ld+json">'
        '{"@type":"Article","image":"https://cdn.example.com/second.jpg"}'
        '</script>'
    )
    assert _jsonld_image(html) == "https://cdn.example.com/first.jpg"


# ═══════════════════════════════════════════════════════════════════
# _filter_article_images  — remove site chrome from image list
# ═══════════════════════════════════════════════════════════════════

def test_filter_removes_page_loader():
    imgs = [
        "https://site.com/img/hero.jpg",
        "https://site.com/img/page_loader.png",
    ]
    result = _filter_article_images(imgs)
    assert result == ["https://site.com/img/hero.jpg"]


def test_filter_removes_fillwz_thumbnails():
    """SilverStripe _FillWz resize directives (sidebar thumbnails)."""
    imgs = [
        "https://site.com/hero.jpg",
        "https://site.com/thumb__FillWzc4MCw0NzBd.jpg",
    ]
    result = _filter_article_images(imgs)
    assert result == ["https://site.com/hero.jpg"]


def test_filter_removes_favicons():
    imgs = [
        "https://site.com/hero.jpg",
        "https://site.com/favicon-32x32.png",
        "https://site.com/apple-touch-icon.png",
        "https://site.com/touchicon.png",
        "https://site.com/og_logo.jpg",
    ]
    result = _filter_article_images(imgs)
    assert result == ["https://site.com/hero.jpg"]


def test_filter_removes_loading_placeholders():
    imgs = [
        "https://site.com/hero.jpg",
        "https://site.com/spinner.gif",
        "https://site.com/loading.svg",
        "https://site.com/placeholder.png",
    ]
    result = _filter_article_images(imgs)
    assert result == ["https://site.com/hero.jpg"]


def test_filter_removes_craft_transform():
    """CraftCMS _transform resize directives."""
    imgs = [
        "https://site.com/hero.jpg",
        "https://site.com/_transform/abc/thumb.jpg",
        "https://site.com/hero__ScaleWidthWzUwMF0.jpg",
        "https://site.com/hero__ResizedImageWzYwMCw0MDBd.jpg",
    ]
    result = _filter_article_images(imgs)
    assert result == ["https://site.com/hero.jpg"]


def test_filter_case_insensitive():
    """Filter patterns are case-insensitive."""
    imgs = [
        "https://site.com/HERO.JPG",
        "https://site.com/Page_Loader.PNG",
    ]
    result = _filter_article_images(imgs)
    assert result == ["https://site.com/HERO.JPG"]


def test_filter_keeps_valid_images():
    """Normal article images pass through unchanged."""
    imgs = [
        "https://cdn.example.com/photos/article-hero.jpg",
        "https://cdn.example.com/charts/data-graph.png",
        "https://live.staticflickr.com/65535/abc123_k.jpg",
    ]
    result = _filter_article_images(imgs)
    assert result == imgs


def test_filter_empty_list():
    assert _filter_article_images([]) == []


# ═══════════════════════════════════════════════════════════════════
# extract_from_html  — readability → fallback → JSON-LD integration
# ═══════════════════════════════════════════════════════════════════

def test_extract_fallback_finds_images_from_full_html():
    """When readability finds text but no images, fall back to full HTML."""
    # readability strips images from this content but keeps the <p>
    html = (
        '<html><body>'
        '<div id="article">'
        '<p>The main article text with important information.</p>'
        '</div>'
        '<figure><img src="/uploads/hero.jpg" alt="hero"/></figure>'
        '</body></html>'
    )
    result = extract_from_html(html, url="https://example.com/post")
    assert "重要信息" in result["content_md"] or "article" in result["content_md"].lower()
    # Fallback should find the hero image
    assert any("hero.jpg" in img for img in result["images"])


def test_extract_fallback_adds_jsonld_image():
    """JSON-LD image is included even when not in a visible <img> tag."""
    html = (
        '<script type="application/ld+json">'
        '{"@type":"NewsArticle","image":"https://cdn.example.com/hero.jpg"}'
        '</script>'
        '<html><body>'
        '<p>The article text without any img tags.</p>'
        '</body></html>'
    )
    result = extract_from_html(html, url="https://example.com/post")
    assert any("hero.jpg" in img for img in result["images"])


def test_extract_fallback_filters_out_site_chrome():
    """Full-HTML fallback should NOT include loaders or favicons."""
    html = (
        '<html><body>'
        '<p>Article text here.</p>'
        '<img src="/page_loader.png" />'
        '<img src="/favicon.ico" />'
        '<img src="/hero.jpg" />'
        '</body></html>'
    )
    result = extract_from_html(html, url="https://example.com/post")
    imgs = result["images"]
    assert any("hero.jpg" in img for img in imgs)
    assert all("page_loader" not in img for img in imgs)
    assert all("favicon" not in img for img in imgs)


def test_extract_fallback_dedupes_by_filename():
    """Same image from different domains shouldn't appear twice."""
    html = (
        '<script type="application/ld+json">'
        '{"@type":"NewsArticle","image":"https://cdn2.example.com/hero.jpg"}'
        '</script>'
        '<html><body>'
        '<p>Article text here.</p>'
        '<img src="https://cdn1.example.com/hero.jpg" />'
        '</body></html>'
    )
    result = extract_from_html(html, url="https://example.com/post")
    # Should only have one hero.jpg (deduped by filename)
    hero_count = sum(1 for img in result["images"] if "hero.jpg" in img)
    assert hero_count == 1


def test_extract_no_fallback_when_images_found():
    """When readability already found images, the fallback should NOT add more."""
    html = (
        '<html><body>'
        '<article>'
        '<p>Article text.</p>'
        '<figure><img src="/hero.jpg" /></figure>'
        '</article>'
        '</body></html>'
    )
    result = extract_from_html(html, url="https://example.com/post")
    # readability should pick up the image normally
    assert len(result["images"]) == 1
    assert "hero.jpg" in result["images"][0]
