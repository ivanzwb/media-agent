"""秀米-style editor components & templates (issue #31).

A *component* is a reusable Markdown snippet (optionally with directive syntax
that :mod:`app.wechat.formatter` renders to inline-CSS HTML) plus metadata
(category, tags). A *template* is a full-article Markdown outline the user can
apply to a draft and then fill in.

Builtin components/templates are defined in Python (so they bundle with the
frozen exe without shipping data files). Users can save their own components,
persisted as JSON in the ``settings`` table.

The directive syntax understood by the formatter (see formatter.py):
  :::tip / :::info / :::warning / :::success / :::danger / :::highlight
  :::center / :::right / :::card / :::footer / :::button / :::tags
  ==highlight==  and  {color:#e67514}文字{/color}  (inline)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.store import Store

CUSTOM_COMPONENTS_KEY = "editor_custom_components"

# Categories (display order)
CATEGORIES = ["标题", "分割线", "卡片", "图文", "引用框", "按钮", "列表", "标签", "页脚"]


@dataclass
class EditorComponent:
    id: str
    name: str
    category: str
    markdown: str
    tags: list[str]
    is_builtin: bool = False

    def to_public(self) -> dict:
        return {
            "id": self.id, "name": self.name, "category": self.category,
            "markdown": self.markdown, "tags": self.tags,
            "is_builtin": self.is_builtin,
        }


@dataclass
class EditorTemplate:
    id: str
    name: str
    markdown: str
    theme: str = "default"
    is_builtin: bool = True

    def to_public(self) -> dict:
        return {"id": self.id, "name": self.name, "markdown": self.markdown,
                "theme": self.theme, "is_builtin": self.is_builtin}


@dataclass
class EditorMaterial:
    id: str
    name: str
    category: str
    markdown: str

    def to_public(self) -> dict:
        return {"id": self.id, "name": self.name, "category": self.category,
                "markdown": self.markdown}


# ── Builtin components (≥30) ─────────────────────────────────────────────
# (id, name, category, markdown, tags)
_C = [
    # 标题 (5)
    ("h2-accent", "强调小标题", "标题", "## {标题文字}\n\n", ["标题"]),
    ("h3-sub", "次级小标题", "标题", "### {小标题}\n\n", ["标题"]),
    ("h2-number", "数字编号标题", "标题", "## 一、{标题文字}\n\n", ["标题", "编号"]),
    ("title-center", "居中大标题", "标题", ":::center\n**{主标题}**\n:::\n\n", ["标题", "居中"]),
    ("title-sub-quote", "标题+副标题", "标题",
     "## {主标题}\n\n:::center\n_{副标题}_\n:::\n\n", ["标题"]),
    # 分割线 (5)
    ("divider-plain", "普通分割线", "分割线", "\n---\n\n", ["分割线"]),
    ("divider-dashed", "虚线分割线", "分割线",
     "\n:::center\n· · · · · · · · · ·\n:::\n\n", ["分割线", "虚线"]),
    ("divider-wave", "波浪分割线", "分割线",
     "\n:::center\n〜〜〜〜〜〜〜〜〜〜\n:::\n\n", ["分割线", "波浪"]),
    ("divider-star", "星星分割线", "分割线",
     "\n:::center\n✦ ✦ ✦\n:::\n\n", ["分割线", "装饰"]),
    ("divider-label", "带文字分割线", "分割线",
     "\n:::center\n—— {分节标题} ——\n:::\n\n", ["分割线", "文字"]),
    # 卡片 (6)
    ("card-tip", "温馨提示卡片", "卡片",
     ":::tip\n💡 **温馨提示**\n{提示内容}\n:::\n\n", ["卡片", "提示"]),
    ("card-info", "信息卡片", "卡片",
     ":::info\nℹ️ **说明**\n{信息内容}\n:::\n\n", ["卡片", "信息"]),
    ("card-warning", "警告卡片", "卡片",
     ":::warning\n⚠️ **注意**\n{警告内容}\n:::\n\n", ["卡片", "警告"]),
    ("card-success", "成功卡片", "卡片",
     ":::success\n✅ **要点**\n{要点内容}\n:::\n\n", ["卡片", "成功"]),
    ("card-danger", "重点卡片", "卡片",
     ":::danger\n🔥 **重点**\n{重点内容}\n:::\n\n", ["卡片", "重点"]),
    ("card-highlight", "高亮方框", "卡片",
     ":::highlight\n{要强调的整段内容}\n:::\n\n", ["卡片", "高亮"]),
    # 图文 (5)
    ("image", "单图", "图文", "![{图片说明}]({图片URL})\n\n", ["图片"]),
    ("image-caption", "图片+说明", "图文",
     "![]({图片URL})\n:::center\n_{图注文字}_\n:::\n\n", ["图片", "图注"]),
    ("image-text", "左图右文", "图文",
     "![]({图片URL})\n\n{配图段落文字}\n\n", ["图文"]),
    ("gallery-2", "两图并排", "图文",
     "| ![]({图1URL}) | ![]({图2URL}) |\n|---|---|\n\n", ["图文", "并排"]),
    ("cover-title", "封面图+标题", "图文",
     "![]({封面URL})\n\n## {文章标题}\n\n", ["图文", "封面"]),
    # 引用框 (3)
    ("quote-simple", "普通引用", "引用框", "> {引用文字}\n\n", ["引用"]),
    ("quote-author", "引用+出处", "引用框",
     "> {引用文字}\n>\n> —— {出处/作者}\n\n", ["引用", "出处"]),
    ("quote-accent", "强调引用框", "引用框",
     ":::card\n> {重点引用内容}\n:::\n\n", ["引用", "强调"]),
    # 按钮 (3)
    ("btn-link", "按钮式链接", "按钮",
     ":::button\n{按钮文字}|{链接URL}\n:::\n\n", ["按钮", "链接"]),
    ("btn-cta", "行动号召按钮", "按钮",
     ":::button\n立即体验|{链接URL}\n:::\n\n", ["按钮", "CTA"]),
    ("btn-follow", "关注引导", "按钮",
     ":::center\n👇 点击下方名片关注我们 👇\n:::\n\n", ["按钮", "关注"]),
    # 列表 (2)
    ("list-bullet", "要点列表", "列表",
     "- {要点一}\n- {要点二}\n- {要点三}\n\n", ["列表"]),
    ("list-number", "步骤列表", "列表",
     "1. {第一步}\n2. {第二步}\n3. {第三步}\n\n", ["列表", "步骤"]),
    # 标签 (2)
    ("tags-row", "标签组", "标签",
     ":::tags\n标签一\n标签二\n标签三\n:::\n\n", ["标签"]),
    ("tag-single", "单个标签", "标签", ":::tags\n{标签文字}\n:::\n\n", ["标签"]),
    # 页脚 (3)
    ("footer-source", "来源页脚", "页脚",
     "\n---\n:::footer\n本文来源：{来源} · 编辑整理\n:::\n\n", ["页脚", "来源"]),
    ("footer-signature", "署名页脚", "页脚",
     "\n---\n:::footer\n关注「{公众号名}」，第一时间获取前沿资讯\n:::\n\n", ["页脚", "署名"]),
    ("footer-disclaimer", "免责声明", "页脚",
     "\n---\n:::footer\n免责声明：本文仅代表作者观点，不构成任何建议。\n:::\n\n",
     ["页脚", "声明"]),
]


def builtin_components() -> list[EditorComponent]:
    return [EditorComponent(id=c[0], name=c[1], category=c[2], markdown=c[3],
                            tags=list(c[4]), is_builtin=True) for c in _C]


# ── Builtin templates (≥10) ──────────────────────────────────────────────
_T: list[tuple[str, str, str, str]] = [
    ("product-launch", "产品发布", "blue",
     "## 一、{产品名}正式发布\n\n:::center\n_{一句话卖点}_\n:::\n\n"
     "![]({封面图URL})\n\n{开头钩子段落}\n\n"
     "## 二、核心亮点\n\n:::success\n✅ **三大突破**\n- {亮点一}\n- {亮点二}\n- {亮点三}\n:::\n\n"
     "## 三、技术细节\n\n{技术说明段落}\n\n"
     "## 四、适用场景\n\n{场景描述}\n\n"
     ":::button\n立即体验|{链接URL}\n:::\n\n"
     "\n---\n:::footer\n关注我们获取更多产品动态\n:::\n"),
    ("deep-tech", "科技深度报道", "default",
     "## 一、{开篇场景/问题}\n\n{钩子段落}\n\n"
     "## 二、背景展开\n\n{背景说明}\n\n"
     ":::tip\n💡 **划重点**\n{关键概念解释}\n:::\n\n"
     "## 三、深入分析\n\n{分析段落}\n\n"
     "## 四、数据支撑\n\n| 指标 | 数值 |\n|---|---|\n| {指标} | {数值} |\n\n"
     "## 写在最后\n\n{总结与展望}\n\n"
     "\n---\n:::footer\n本文来源：{来源}\n:::\n"),
    ("festival", "节日祝福", "warm",
     ":::center\n**🎉 {节日名}快乐 🎉**\n:::\n\n"
     "![]({节日配图URL})\n\n{祝福开场白}\n\n"
     ":::highlight\n{核心祝福语}\n:::\n\n"
     "{正文段落}\n\n"
     ":::center\n〜〜〜〜〜〜〜〜〜〜\n:::\n\n"
     ":::footer\n{公众号名} 全体成员敬上\n:::\n"),
    ("event", "活动预告", "warm",
     "## 🎪 {活动名称}\n\n![]({活动海报URL})\n\n"
     ":::info\nℹ️ **活动信息**\n- 🕐 时间：{时间}\n- 📍 地点：{地点}\n- 🎟️ 报名：{报名方式}\n:::\n\n"
     "## 活动亮点\n\n- {亮点一}\n- {亮点二}\n- {亮点三}\n\n"
     "{活动详情段落}\n\n"
     ":::button\n立即报名|{报名链接}\n:::\n"),
    ("recruit", "招聘启事", "blue",
     "## 💼 {公司名} 招聘\n\n{公司简介}\n\n"
     "## 岗位：{岗位名称}\n\n:::info\nℹ️ **岗位职责**\n- {职责一}\n- {职责二}\n:::\n\n"
     ":::success\n✅ **任职要求**\n- {要求一}\n- {要求二}\n:::\n\n"
     "## 我们提供\n\n- {福利一}\n- {福利二}\n\n"
     ":::button\n投递简历|{投递方式}\n:::\n\n"
     ":::footer\n期待你的加入！\n:::\n"),
    ("tutorial", "教程指南", "default",
     "## {教程标题}\n\n:::tip\n💡 本文将带你 {目标}\n:::\n\n"
     "## 准备工作\n\n- {前置条件一}\n- {前置条件二}\n\n"
     "## 步骤一：{步骤标题}\n\n{步骤说明}\n\n"
     "## 步骤二：{步骤标题}\n\n{步骤说明}\n\n"
     "## 步骤三：{步骤标题}\n\n{步骤说明}\n\n"
     ":::success\n✅ **完成！**\n{结果说明}\n:::\n"),
    ("review", "产品评测", "default",
     "## {产品名}评测：{一句话结论}\n\n![]({产品图URL})\n\n{开场段落}\n\n"
     "## 外观与设计\n\n{外观评价}\n\n"
     "## 使用体验\n\n{体验评价}\n\n"
     "## 优缺点\n\n:::success\n✅ **优点**\n- {优点一}\n- {优点二}\n:::\n\n"
     ":::danger\n🔥 **不足**\n- {缺点一}\n:::\n\n"
     "## 购买建议\n\n{购买建议}\n"),
    ("news", "新闻快讯", "default",
     "## {新闻标题}\n\n:::info\nℹ️ {导语一句话}\n:::\n\n"
     "{正文第一段}\n\n{正文第二段}\n\n"
     "> {关键人物引语}\n\n{背景补充}\n\n"
     "\n---\n:::footer\n来源：{来源} · {日期}\n:::\n"),
    ("list-article", "盘点清单", "warm",
     "## {数字}个{主题}盘点\n\n{开场白}\n\n"
     "## 1. {条目一}\n\n{说明}\n\n"
     "## 2. {条目二}\n\n{说明}\n\n"
     "## 3. {条目三}\n\n{说明}\n\n"
     ":::center\n· · · · · · · · · ·\n:::\n\n"
     "## 写在最后\n\n{总结}\n"),
    ("interview", "人物访谈", "warm",
     "## 专访 {人物名}\n\n![]({人物照片URL})\n\n:::center\n_{人物一句话简介}_\n:::\n\n"
     "{开场介绍}\n\n"
     "## Q：{问题一}\n\n> {回答一}\n\n"
     "## Q：{问题二}\n\n> {回答二}\n\n"
     "## Q：{问题三}\n\n> {回答三}\n\n"
     ":::footer\n本文由 {公众号名} 原创\n:::\n"),
    ("weekly", "周报/月报", "blue",
     "## {期号} {团队名}周报\n\n:::highlight\n本周关键词：{关键词}\n:::\n\n"
     "## 📈 本周进展\n\n- {进展一}\n- {进展二}\n\n"
     "## 🎯 下周计划\n\n1. {计划一}\n2. {计划二}\n\n"
     "## 💬 其他\n\n{其他事项}\n\n"
     ":::footer\n{团队名} · {日期}\n:::\n"),
]


def builtin_templates() -> list[EditorTemplate]:
    return [EditorTemplate(id=t[0], name=t[1], theme=t[2], markdown=t[3],
                           is_builtin=True) for t in _T]


# ── Materials (icons / decorative snippets) ──────────────────────────────
_M: list[tuple[str, str, str, str]] = [
    ("icon-fire", "火焰", "图标", "🔥"),
    ("icon-star", "星星", "图标", "⭐"),
    ("icon-check", "对勾", "图标", "✅"),
    ("icon-point", "手指", "图标", "👉"),
    ("icon-bulb", "灯泡", "图标", "💡"),
    ("icon-rocket", "火箭", "图标", "🚀"),
    ("deco-dots", "圆点分隔", "装饰", "· · · · · · · · · ·"),
    ("deco-wave", "波浪分隔", "装饰", "〜〜〜〜〜〜〜〜〜〜"),
    ("deco-stars", "三星分隔", "装饰", "✦ ✦ ✦"),
    ("deco-arrow", "向下箭头", "装饰", "👇"),
]


def materials() -> list[EditorMaterial]:
    return [EditorMaterial(id=m[0], name=m[1], category=m[2], markdown=m[3])
            for m in _M]


# ── Custom components (user-defined) ─────────────────────────────────────

def _load_custom_raw(store: "Store") -> list[dict]:
    raw = store.get_setting(CUSTOM_COMPONENTS_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


def custom_components(store: "Store") -> list[EditorComponent]:
    out: list[EditorComponent] = []
    for d in _load_custom_raw(store):
        try:
            out.append(EditorComponent(
                id=str(d["id"]), name=str(d.get("name") or d["id"]),
                category=str(d.get("category") or "自定义"),
                markdown=str(d.get("markdown") or ""),
                tags=list(d.get("tags") or []), is_builtin=False))
        except (KeyError, TypeError):
            continue
    return out


def _builtin_component_ids() -> set[str]:
    return {c[0] for c in _C}


def all_components(store: "Store | None" = None) -> list[EditorComponent]:
    comps = builtin_components()
    if store is not None:
        comps += custom_components(store)
    return comps


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "custom"


def save_custom_component(store: "Store", *, id: str | None, name: str,
                          category: str, markdown: str,
                          tags: list[str] | None = None) -> EditorComponent:
    name = (name or "").strip()
    if not name:
        raise ValueError("组件名称不能为空")
    markdown = markdown or ""
    if not markdown.strip():
        raise ValueError("组件内容不能为空")
    category = (category or "自定义").strip() or "自定义"
    tags = tags or []

    items = _load_custom_raw(store)
    if id:
        sid = str(id).strip()
        if sid in _builtin_component_ids():
            raise ValueError("不能覆盖内置组件")
        found = False
        for it in items:
            if it.get("id") == sid:
                it.update(name=name, category=category, markdown=markdown,
                          tags=tags)
                found = True
                break
        if not found:
            raise ValueError(f"组件不存在：{sid}")
    else:
        base = _slugify(name)
        sid = base
        existing = _builtin_component_ids() | {it.get("id") for it in items}
        i = 2
        while sid in existing:
            sid = f"{base}-{i}"
            i += 1
        items.append({"id": sid, "name": name, "category": category,
                      "markdown": markdown, "tags": tags})
    _save_custom_raw(store, items)
    return EditorComponent(id=sid, name=name, category=category,
                           markdown=markdown, tags=tags, is_builtin=False)


def _save_custom_raw(store: "Store", items: list[dict]) -> None:
    if items:
        store.set_setting(CUSTOM_COMPONENTS_KEY,
                          json.dumps(items, ensure_ascii=False))
    else:
        store.delete_setting(CUSTOM_COMPONENTS_KEY)


def delete_custom_component(store: "Store", comp_id: str) -> bool:
    sid = str(comp_id).strip()
    if sid in _builtin_component_ids():
        raise ValueError("不能删除内置组件")
    items = _load_custom_raw(store)
    new_items = [it for it in items if it.get("id") != sid]
    if len(new_items) == len(items):
        return False
    _save_custom_raw(store, new_items)
    return True


def get_template(template_id: str) -> EditorTemplate | None:
    for t in builtin_templates():
        if t.id == template_id:
            return t
    return None
