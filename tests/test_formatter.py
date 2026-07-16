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


def test_columns_render_flex_row():
    """::::columns → a flex <section> row with one flex child per :::col."""
    md = ("::::columns\n:::col\n左边\n:::\n:::col\n右边\n:::\n::::")
    html = render_styled_html(md)
    assert "display:flex" in html
    # two column sections, equal width by default
    assert html.count("flex:1;min-width:0;") == 2
    assert "左边" in html and "右边" in html


def test_columns_custom_ratio():
    md = ("::::columns 1:2\n:::col\nA\n:::\n:::col\nB\n:::\n::::")
    html = render_styled_html(md)
    assert "flex:1;min-width:0;" in html
    assert "flex:2;min-width:0;" in html


def test_columns_cols_param_three_equal():
    md = ("::::columns cols=3\n:::col\nA\n:::\n:::col\nB\n:::\n:::col\nC\n:::\n::::")
    html = render_styled_html(md)
    assert html.count("flex:1;min-width:0;") == 3


def test_columns_nested_directive_and_inline():
    """A column body may hold markdown + inline syntaxes + a nested :::directive."""
    md = ("::::columns\n:::col\n==高亮== 与 {color:#e60000}红字{/color}\n"
          ":::tip\n提示\n:::\n:::\n:::col\n右边\n:::\n::::")
    html = render_styled_html(md)
    assert "display:flex" in html
    assert "<mark" in html and "color:#e60000" in html
    # nested tip card rendered inside the first column
    assert "border-left:4px solid #67c23a" in html
    assert "提示" in html


def test_dashed_and_gradient_dividers():
    dashed = render_styled_html(":::dashed\n:::")
    assert "border-top:1px dashed #c8c8c8" in dashed
    grad = render_styled_html(":::gradient\n:::")
    assert "linear-gradient(to right,rgba(64,158,255,0),#409eff,rgba(64,158,255,0))" in grad


def test_blocktitle_and_dualline():
    block = render_styled_html(":::blocktitle\n标题文字\n:::")
    assert "background:#2f6fb3" in block and "标题文字" in block
    dual = render_styled_html(":::dualline\n标题文字\n:::")
    assert "border-top:2px solid #333333" in dual
    assert "border-bottom:2px solid #333333" in dual and "标题文字" in dual


def test_titlenum_badge_and_title():
    html = render_styled_html(":::titlenum\n01 | 章节标题\n:::")
    assert "border-radius:13px" in html          # circular number badge
    assert ">01</span>" in html                    # badge number
    assert "章节标题" in html


def test_imgcard_frame_image_and_caption():
    md = ":::imgcard\n![猫](/img/cat.png)\n可爱的猫\n:::"
    html = render_styled_html(md)
    assert "box-shadow:0 2px 12px rgba(0,0,0,0.08)" in html   # framed card
    assert '<img src="/img/cat.png"' in html
    assert "width:100%;margin:0;border-radius:0;" in html     # image fills frame
    assert "可爱的猫" in html                                  # caption text


def test_imgcard_without_caption():
    html = render_styled_html(":::imgcard\n![](/img/a.png)\n:::")
    assert '<img src="/img/a.png"' in html
    assert "font-size:13px;color:#888888" not in html         # no caption <p>


def test_card_directive_matches_preview_style():
    html = render_styled_html(":::card\n卡片内容\n:::")
    assert "border:1px solid #e6e8eb" in html and "border-radius:8px" in html
    assert "卡片内容" in html


def test_toutiao_theme_and_listing():
    html = render_styled_html("## T\n\n正文", platform="toutiao")
    assert "<section style=" in html and "#f04142" in html  # toutiao red
    ids = [t["id"] for t in list_themes("wechat")]
    assert "default" in ids and "blue" in ids
    assert [t["id"] for t in list_themes("toutiao")] == ["default"]
