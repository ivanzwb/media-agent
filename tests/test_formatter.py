from app.wechat.formatter import render_styled_html, list_themes


MD = (
    "# 大标题\n\n"
    "## 小节\n\n"
    "正文 **加粗** 与 `代码` 和[链接](https://e.com)。\n\n"
    "- 一\n- 二\n\n"
    "> 引用一句\n\n"
    "```python\nprint('hi')\nx = 1\n```\n\n"
    "![图](/images/p.png)\n"
)


def test_render_wraps_in_section_with_inline_styles():
    html = render_styled_html(MD, platform="wechat", theme="default")
    assert html.startswith("<section style=")
    # headings/paragraphs carry inline styles (WeChat strips classes/<style>)
    assert "<h1 style=" in html and "<h2 style=" in html
    assert "<p style=" in html
    assert "<strong>加粗</strong>" in html
    assert "<code style=" in html            # inline code styled
    assert '<a href="https://e.com" style=' in html
    assert "<ul style=" in html and "<li style=" in html
    assert "<blockquote style=" in html
    assert '<img src="/images/p.png"' in html and "style=" in html
    assert "<style" not in html              # no <style> blocks (inlined)


def test_code_block_uses_br_and_prewrap():
    html = render_styled_html(MD, platform="wechat", theme="default")
    assert "<pre style=" in html and "white-space:pre-wrap" in html
    # code lines joined with <br/> (editors collapse raw newlines in <pre>)
    assert "print(&#x27;hi&#x27;)<br/>x = 1" in html


def test_themes_differ_by_accent():
    default = render_styled_html("## 标题", platform="wechat", theme="default")
    blue = render_styled_html("## 标题", platform="wechat", theme="blue")
    assert "#07C160" in default            # green accent
    assert "#2f6fb3" in blue               # blue accent
    assert default != blue


def test_tweaks_override_accent_and_font():
    html = render_styled_html("## 标题\n\n正文", platform="wechat",
                              theme="default",
                              tweaks={"accent": "# abc123".replace(" ", ""),
                                      "font_size": 20})
    assert "#abc123" in html
    assert "font-size:20px" in html        # base section font from tweak


def test_toutiao_theme_and_listing():
    html = render_styled_html("## T\n\n正文", platform="toutiao")
    assert "<section style=" in html and "#f04142" in html  # toutiao red
    ids = [t["id"] for t in list_themes("wechat")]
    assert "default" in ids and "blue" in ids
    assert [t["id"] for t in list_themes("toutiao")] == ["default"]
