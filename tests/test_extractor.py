from pathlib import Path

from app.sources.extractor import (
    extract_from_html, _videos_with_placeholders, _images_from_html,
    _videos_from_html, _date_from_html)

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
