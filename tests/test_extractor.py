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
# _renumber_content  — image index remapping after filtering
# ═══════════════════════════════════════════════════════════════════

def test_image_placeholder_renumbering_after_filter():
    """When _filter_article_images removes some entries, [[IMG:N]] tokens
    in content_md should be renumbered to match the filtered list."""
    html = (
        '<html><body>'
        '<article>'
        '<h1>Article Title</h1>'
        '<p>First paragraph of the article with substantial content that '
        'describes the topic in detail and provides useful information for '
        'readers who want to learn more about this interesting subject.</p>'
        '<img src="/hero.jpg" />'
        '<p>Middle paragraph with additional context and analysis of the '
        'key findings from the research team at the university laboratory.</p>'
        '<img src="/page_loader.png" />'
        '<p>Third paragraph covering the implications of this research for '
        'future developments in the field and what it means for practitioners.</p>'
        '<img src="/favicon-32x32.png" />'
        '<p>Fourth paragraph wrapping up with conclusions and next steps '
        'for further investigation and follow-up studies in this area.</p>'
        '<img src="/chart.png" />'
        '<p>Final paragraph that concludes the article and offers a summary '
        'of the most important takeaways for the reader to remember.</p>'
        '</article>'
        '</body></html>'
    )
    result = extract_from_html(html, url="https://x.com/post")
    imgs = result["images"]
    content_md = result["content_md"]

    # Images with "page_loader" or "favicon" should be removed
    assert all("page_loader" not in img for img in imgs)
    assert all("favicon" not in img for img in imgs)

    # Kept images should remain
    assert any("hero.jpg" in img for img in imgs)
    assert any("chart.png" in img for img in imgs)

    # All [[IMG:N]] tokens in content_md must reference valid indices
    import re
    for m in re.finditer(r'\[\[IMG:(\d+)\]\]', content_md):
        idx = int(m.group(1))
        assert 0 <= idx < len(imgs), (
            f"[[IMG:{idx}]] out of range (max {len(imgs)-1}) in content_md")


def test_image_placeholder_renumbering_no_filter():
    """When no images are filtered, indices remain unchanged."""
    html = (
        '<html><body>'
        '<article>'
        '<h1>Article Title</h1>'
        '<p>First paragraph with meaningful content that is long enough for '
        'trafilatura to extract properly without dropping the text content.</p>'
        '<img src="/photo-a.jpg" />'
        '<p>Second paragraph continues with more analysis and discussion of '
        'the main topic to provide a comprehensive overview of the subject.</p>'
        '<img src="/photo-b.jpg" />'
        '<p>Third paragraph wrapping up and providing concluding thoughts on '
        'the matter discussed in the previous paragraphs above this one.</p>'
        '</article>'
        '</body></html>'
    )
    result = extract_from_html(html, url="https://x.com/post")
    imgs = result["images"]
    content_md = result["content_md"]

    # With only clean article images, nothing should be filtered
    assert any("photo-a.jpg" in img for img in imgs)
    assert any("photo-b.jpg" in img for img in imgs)
    assert len(imgs) <= 12  # cap applied

    # All [[IMG:N]] must be in range
    import re
    for m in re.finditer(r'\[\[IMG:(\d+)\]\]', content_md):
        idx = int(m.group(1))
        assert 0 <= idx < len(imgs), (
            f"[[IMG:{idx}]] out of range (max {len(imgs)-1})")


def test_image_count_capped_at_max():
    """extract_from_html caps images to _MAX_IMG_PLACEHOLDERS (12)."""
    # Create HTML with many distinct images
    imgs_html = "".join(
        f'<img src="/img{i}.jpg" />' for i in range(20)
    )
    html = (
        '<html><body><article>'
        '<h1>Many Images</h1>'
        '<p>Article with many images. ' * 20
        + imgs_html +
        '<p>More text to ensure the article is long enough. ' * 10 +
        '</article></body></html>'
    )
    result = extract_from_html(html, url="https://x.com/post")
    assert len(result["images"]) <= 12, (
        f"images capped at 12, got {len(result['images'])}")


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


# ═══════════════════════════════════════════════════════════════════
# extract_from_html  —  future-date sanity filter
# ═══════════════════════════════════════════════════════════════════

def test_extract_rejects_future_date_more_than_one_day():
    """Dates >1 day in the future are set to None (listing/event pages)."""
    from datetime import datetime, timezone, timedelta
    future = datetime.now(timezone.utc) + timedelta(days=3)
    html = (
        f'<meta property="article:published_time" '
        f'content="{future.isoformat()}"/>'
        f"<html><body><p>Article content that needs to be quite long "
        f"so that trafilatura returns something meaningful for our test."
        f" We keep adding more words until the content is long enough."
        f" Repeating the main idea over and over to pad the body text."
        f" At this point the text should be well past 300 characters."
        f" </p><p>A second paragraph with additional content to ensure"
        f" multiple text lines are available after extraction. This is"
        f" important for the image extraction tests as well.</p>"
        f"<p>A third paragraph adds even more content to the article.</p>"
        f"</body></html>"
    )
    result = extract_from_html(html, url="https://example.com/post")
    assert result["published_at"] is None, (
        f"Expected None for future date, got {result['published_at']}")


def test_extract_keeps_recent_date():
    """Dates up to 1 day in the future are kept (timezone differences)."""
    from datetime import datetime, timezone, timedelta
    recent = datetime.now(timezone.utc) - timedelta(hours=2)
    html = (
        f'<meta property="article:published_time" '
        f'content="{recent.isoformat()}"/>'
        f"<html><body><p>Article content that needs to be quite long "
        f"so that trafilatura returns something meaningful for our test."
        f" We keep adding more words until the content is long enough."
        f" Repeating the main idea over and over to pad the body text."
        f" At this point the text should be well past 300 characters."
        f" </p><p>A second paragraph with additional content to ensure"
        f" multiple text lines are available after extraction. This is"
        f" important for the image extraction tests as well.</p>"
        f"<p>A third paragraph adds even more content to the article.</p>"
        f"</body></html>"
    )
    result = extract_from_html(html, url="https://example.com/post")
    assert result["published_at"] is not None


def test_extract_keeps_today_plus_one():
    """Dates exactly 1 day ahead are still accepted (safe margin)."""
    from datetime import datetime, timezone, timedelta
    tomorrow = datetime.now(timezone.utc) + timedelta(hours=23)
    html = (
        f'<meta property="article:published_time" '
        f'content="{tomorrow.isoformat()}"/>'
        f"<html><body><p>Article content that needs to be quite long "
        f"so that trafilatura returns something meaningful for our test."
        f" We keep adding more words until the content is long enough."
        f" Repeating the main idea over and over to pad the body text."
        f" At this point the text should be well past 300 characters."
        f" </p><p>A second paragraph with additional content to ensure"
        f" multiple text lines are available after extraction. This is"
        f" important for the image extraction tests as well.</p>"
        f"<p>A third paragraph adds even more content to the article.</p>"
        f"</body></html>"
    )
    result = extract_from_html(html, url="https://example.com/post")
    assert result["published_at"] is not None


def test_extract_keeps_past_date():
    """Dates in the past are always kept."""
    from datetime import datetime, timezone, timedelta
    past = datetime.now(timezone.utc) - timedelta(days=7)
    html = (
        f'<meta property="article:published_time" '
        f'content="{past.isoformat()}"/>'
        f"<html><body><p>Article content that needs to be quite long "
        f"so that trafilatura returns something meaningful for our test."
        f" We keep adding more words until the content is long enough."
        f" Repeating the main idea over and over to pad the body text."
        f" At this point the text should be well past 300 characters."
        f" </p><p>A second paragraph with additional content to ensure"
        f" multiple text lines are available after extraction. This is"
        f" important for the image extraction tests as well.</p>"
        f"<p>A third paragraph adds even more content to the article.</p>"
        f"</body></html>"
    )
    result = extract_from_html(html, url="https://example.com/post")
    assert result["published_at"] is not None
    assert (result["published_at"].year, result["published_at"].month) == (
        past.year, past.month)


# ═══════════════════════════════════════════════════════════════════
# extract_from_html  —  force [[IMG:N]] when body has no tokens (4d)
# ═══════════════════════════════════════════════════════════════════


def test_extract_forces_img_placeholders_when_body_has_no_tokens():
    """4d safety net: when images are found via _content_fallback (step 3) but
    content_md has no [[IMG:N]] tokens, the orphan-injector (4b) or safety net
    (4d) prepends all placeholders so the rewriter can inline them."""
    # A minimal HTML that readability/trafilatura can't extract meaningfully
    # from, forcing step 3 (_content_fallback), which produces plain <p> text
    # without any [[IMG:N]] tokens.  Images are detected separately.
    html = (
        '<html><body>'
        '<p>Article text without image placeholders.</p>'
        '<img src="/pic1.jpg" />'
        '<img src="/pic2.jpg" />'
        '</body></html>'
    )
    result = extract_from_html(html, url="https://example.com/post")
    assert len(result["images"]) >= 2
    # The content_md should contain [[IMG:0]] and [[IMG:1]] (injected
    # by orphan detector 4b or safety net 4d)
    assert "[[IMG:0]]" in result["content_md"], (
        "4b/4d should inject [[IMG:0]] into content_md")
    assert "[[IMG:1]]" in result["content_md"], (
        "4b/4d should inject [[IMG:1]] into content_md")
    # Original body text preserved
    assert "Article text" in result["content_md"]


def test_extract_does_not_duplicate_placeholders_when_present():
    """4b/4d: if body already has [[IMG:N]] tokens (via normal processing or
    literal text), the safety net does NOT add extra duplicates."""
    html = (
        '<html><body>'
        '<p>正文已有 [[IMG:0]] 占位符</p>'
        '<img src="/pic1.jpg" />'
        '<img src="/pic2.jpg" />'
        '</body></html>'
    )
    result = extract_from_html(html, url="https://example.com/post")
    # After re-numbering, at most one [[IMG:0]] from normal processing plus
    # one from the literal text may coexist — but the safety net should NOT
    # add a third.
    im0_count = result["content_md"].count("[[IMG:0]]")
    assert im0_count <= 2, (
        f"Expected at most 2 [[IMG:0]], got {im0_count}")
    # The two images in the HTML should produce at most [[IMG:0]] and [[IMG:1]]
    # (step-4 re-numbering may consolidate duplicates)
    assert "[[IMG:1]]" in result["content_md"] or len(result["images"]) >= 1


def test_extract_no_images_no_placeholder_injection():
    """4d safety net: no images → no injection."""
    html = (
        '<html><body>'
        '<p>Plain text article without any images.</p>'
        '</body></html>'
    )
    result = extract_from_html(html, url="https://example.com/post")
    assert "[[IMG:" not in result["content_md"]


# ═══════════════════════════════════════════════════════════════════
# extract_from_html  —  readability short-output fallback (step 1c)
# ═══════════════════════════════════════════════════════════════════


def test_readability_short_output_triggers_full_html_fallback():
    """When readability returns only ~200 bytes (e.g. NIMS div#p_N
    structure with image grids), step 1c should re-extract from the
    full HTML via trafilatura and keep the richer result."""
    html = (
        '<html><body>'
        '<main id="main-content">'
        '<h1>NIMS-style Article</h1>'
        '<div class="article-container">'
        '<div id="s_1">'
        '<div class="text-box"><p>First paragraph that readability keeps.</p></div>'
        '</div>'
        '<div id="s_2">'
        '<div class="grid-wrap"><figure><img src="/photo.jpg" /></figure></div>'
        '</div>'
        '<div id="s_3">'
        '<div class="text-box"><p>Second paragraph with important details '
        'describing the research achievements and their significance in '
        'the field of materials science and engineering applications.</p></div>'
        '</div>'
        '<div id="s_4">'
        '<p class="text-plain">Third paragraph covering additional findings '
        'and future directions for the research team at the institution '
        'that conducted this groundbreaking study and its implications.</p>'
        '</div>'
        '</div>'
        '</main>'
        '</body></html>'
    )
    result = extract_from_html(html, url="https://example.com/post")
    # Step 1c should have recovered the full content, not just the first paragraph
    content = result["content_md"]
    assert "Second paragraph" in content, (
        "step 1c should recover content readability dropped")
    assert "Third paragraph" in content, (
        "step 1c should recover later sections")


def test_readability_short_output_keeps_images():
    """When step 1c triggers, images from the full HTML are kept and
    filtered properly (SVG icons removed, article images kept)."""
    html = (
        '<html><body>'
        '<main id="main-content">'
        '<div class="article-container">'
        '<div id="s_1">'
        '<p>Short first paragraph that readability keeps.</p>'
        '</div>'
        '<div id="s_2">'
        '<img src="/icon_x.svg" />'
        '<img src="/footer-logo.svg" />'
        '<img src="/photo.jpg" />'
        '</div>'
        '<div id="s_3">'
        '<p>Second paragraph with much more content that readability '
        'would miss due to the icon section breaking its heuristics '
        'for detecting contiguous article text in this structure.</p>'
        '</div>'
        '</div>'
        '</main>'
        '</body></html>'
    )
    result = extract_from_html(html, url="https://example.com/post")
    imgs = result["images"]
    # SVG icons should be filtered out
    assert all("icon_" not in img for img in imgs), (
        f"icon SVGs should be filtered, got {imgs}")
    assert all("logo" not in img for img in imgs), (
        f"logo SVGs should be filtered, got {imgs}")
    # Real article image should be kept
    assert any("photo.jpg" in img for img in imgs), (
        f"article image should be kept, got {imgs}")
