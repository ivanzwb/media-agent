from pathlib import Path

from app.sources.extractor import (
    extract_from_html, _videos_with_placeholders, _images_from_html,
    _videos_from_html)

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
