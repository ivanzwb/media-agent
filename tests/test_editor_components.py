"""Tests for the 秀米-style editor components/templates (issue #31)."""
import pytest

from app.wechat import components as C
from app.wechat.formatter import render_styled_html


class FakeStore:
    def __init__(self):
        self.d: dict[str, str] = {}

    def get_setting(self, key, default=None):
        return self.d.get(key, default)

    def set_setting(self, key, value):
        self.d[key] = value

    def delete_setting(self, key):
        self.d.pop(key, None)


# ── Builtin data ─────────────────────────────────────────────────────────

def test_at_least_30_builtin_components():
    comps = C.builtin_components()
    assert len(comps) >= 30
    ids = [c.id for c in comps]
    assert len(ids) == len(set(ids))  # unique
    assert all(c.is_builtin for c in comps)


def test_category_coverage_meets_acceptance():
    comps = C.builtin_components()
    by_cat: dict[str, int] = {}
    for c in comps:
        by_cat[c.category] = by_cat.get(c.category, 0) + 1
    # acceptance minimums
    assert by_cat.get("标题", 0) >= 5
    assert by_cat.get("分割线", 0) >= 5
    assert by_cat.get("卡片", 0) >= 5
    assert by_cat.get("图文", 0) >= 5
    assert by_cat.get("引用框", 0) >= 3
    assert by_cat.get("按钮", 0) >= 3
    assert by_cat.get("列表", 0) >= 2
    assert by_cat.get("页脚", 0) >= 2


def test_at_least_10_builtin_templates():
    tpls = C.builtin_templates()
    assert len(tpls) >= 10
    assert all(t.markdown.strip() for t in tpls)


def test_materials_present():
    assert len(C.materials()) >= 5


def test_get_template():
    assert C.get_template("product-launch") is not None
    assert C.get_template("nope") is None


# ── Custom component CRUD ─────────────────────────────────────────────────

def test_create_custom_component():
    st = FakeStore()
    c = C.save_custom_component(st, id=None, name="我的卡片",
                               category="自定义", markdown=":::tip\nhi\n:::")
    assert c.id and not c.is_builtin
    allc = C.all_components(st)
    assert any(x.id == c.id for x in allc)
    assert len(allc) == len(C.builtin_components()) + 1


def test_custom_requires_name_and_markdown():
    st = FakeStore()
    with pytest.raises(ValueError):
        C.save_custom_component(st, id=None, name="", category="",
                               markdown="x")
    with pytest.raises(ValueError):
        C.save_custom_component(st, id=None, name="x", category="",
                               markdown="   ")


def test_custom_unique_ids():
    st = FakeStore()
    a = C.save_custom_component(st, id=None, name="Box", category="c", markdown="a")
    b = C.save_custom_component(st, id=None, name="Box", category="c", markdown="b")
    assert a.id != b.id


def test_cannot_overwrite_or_delete_builtin():
    st = FakeStore()
    with pytest.raises(ValueError):
        C.save_custom_component(st, id="card-tip", name="x", category="c",
                               markdown="y")
    with pytest.raises(ValueError):
        C.delete_custom_component(st, "card-tip")


def test_update_and_delete_custom():
    st = FakeStore()
    a = C.save_custom_component(st, id=None, name="Box", category="c", markdown="a")
    C.save_custom_component(st, id=a.id, name="Box2", category="c2", markdown="bb")
    got = [x for x in C.custom_components(st) if x.id == a.id][0]
    assert got.name == "Box2" and got.markdown == "bb"
    assert C.delete_custom_component(st, a.id) is True
    assert C.delete_custom_component(st, a.id) is False


# ── Custom template CRUD ──────────────────────────────────────────────────

def test_create_custom_template():
    st = FakeStore()
    t = C.save_custom_template(st, id=None, name="我的模板",
                               markdown="# 标题\n\n正文")
    assert t.id and not t.is_builtin
    allt = C.all_templates(st)
    assert any(x.id == t.id for x in allt)
    assert len(allt) == len(C.builtin_templates()) + 1
    # resolvable via get_template with a store
    assert C.get_template(t.id, st) is not None
    assert C.get_template(t.id) is None  # builtin-only lookup


def test_custom_template_requires_name_and_markdown():
    st = FakeStore()
    with pytest.raises(ValueError):
        C.save_custom_template(st, id=None, name="", markdown="x")
    with pytest.raises(ValueError):
        C.save_custom_template(st, id=None, name="x", markdown="   ")


def test_custom_template_unique_ids():
    st = FakeStore()
    a = C.save_custom_template(st, id=None, name="Tpl", markdown="a")
    b = C.save_custom_template(st, id=None, name="Tpl", markdown="b")
    assert a.id != b.id


def test_cannot_overwrite_or_delete_builtin_template():
    st = FakeStore()
    with pytest.raises(ValueError):
        C.save_custom_template(st, id="product-launch", name="x", markdown="y")
    with pytest.raises(ValueError):
        C.delete_custom_template(st, "product-launch")


def test_update_and_delete_custom_template():
    st = FakeStore()
    a = C.save_custom_template(st, id=None, name="Tpl", markdown="a")
    C.save_custom_template(st, id=a.id, name="Tpl2", markdown="bb")
    got = [x for x in C.custom_templates(st) if x.id == a.id][0]
    assert got.name == "Tpl2" and got.markdown == "bb"
    assert C.delete_custom_template(st, a.id) is True
    assert C.delete_custom_template(st, a.id) is False


# ── Formatter directive rendering ─────────────────────────────────────────

def test_formatter_renders_tip_card():
    html = render_styled_html(":::tip\n💡 提示\n内容\n:::", theme="default")
    assert "<section" in html
    assert "border-left" in html
    assert "提示" in html


def test_formatter_renders_button():
    html = render_styled_html(":::button\n立即体验|https://example.com\n:::")
    assert 'href="https://example.com"' in html
    assert "立即体验" in html
    assert "border-radius:24px" in html


def test_formatter_renders_tags():
    html = render_styled_html(":::tags\nAI\n大模型\n:::")
    assert html.count("border-radius:12px") >= 2
    assert "AI" in html and "大模型" in html


def test_formatter_center_and_footer():
    html = render_styled_html(":::center\n居中\n:::\n\n:::footer\n页脚\n:::")
    assert "text-align:center" in html
    assert "居中" in html and "页脚" in html


def test_formatter_inline_highlight_and_color():
    html = render_styled_html("普通 ==高亮== 与 {color:#e67514}彩色{/color} 文字")
    assert "<mark" in html and "高亮" in html
    assert "color:#e67514" in html and "彩色" in html


def test_formatter_nested_directive():
    md = ":::card\n:::center\n嵌套\n:::\n:::"
    html = render_styled_html(md)
    assert "嵌套" in html
    assert "text-align:center" in html


def test_all_builtin_components_render_without_error():
    """Every builtin component's markdown must render to HTML cleanly."""
    for c in C.builtin_components():
        html = render_styled_html(c.markdown)
        assert html.startswith("<section")


def test_all_builtin_templates_render_without_error():
    for t in C.builtin_templates():
        html = render_styled_html(t.markdown, theme=t.theme)
        assert "<section" in html
