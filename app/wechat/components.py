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
CUSTOM_TEMPLATES_KEY = "editor_custom_templates"

# Categories (display order)
CATEGORIES = ["标题", "分割线", "卡片", "图文", "布局", "引用框", "按钮", "列表", "标签", "页脚"]


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
    category: str = "通用"
    is_builtin: bool = True

    def to_public(self) -> dict:
        return {"id": self.id, "name": self.name, "markdown": self.markdown,
                "theme": self.theme, "category": self.category,
                "is_builtin": self.is_builtin}


@dataclass
class EditorMaterial:
    id: str
    name: str
    category: str
    markdown: str

    def to_public(self) -> dict:
        return {"id": self.id, "name": self.name, "category": self.category,
                "markdown": self.markdown}


# A self-contained SVG placeholder so image components actually render in the
# WYSIWYG preview (and gallery thumbnails) before the user swaps in a real URL.
EXAMPLE_IMG = (
    "data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20"
    "width='640'%20height='360'%3E%3Crect%20width='640'%20height='360'%20"
    "fill='%23eef3fb'/%3E%3Crect%20x='0.5'%20y='0.5'%20width='639'%20"
    "height='359'%20fill='none'%20stroke='%23b9d4f2'/%3E%3Ctext%20x='320'%20"
    "y='196'%20font-family='sans-serif'%20font-size='34'%20fill='%232f6fb3'%20"
    "text-anchor='middle'%3E%E7%A4%BA%E4%BE%8B%E5%9B%BE%E7%89%87%20640x360"
    "%3C/text%3E%3C/svg%3E"
)

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
    ("title-dual-line", "双线标题", "标题",
     ":::dualline\n{标题文字}\n:::\n\n", ["标题", "双线", "装饰"]),
    ("title-block", "色块标题", "标题",
     ":::blocktitle\n{标题文字}\n:::\n\n", ["标题", "色块", "装饰"]),
    ("title-number", "序号标题", "标题",
     ":::titlenum\n01 | {标题文字}\n:::\n\n", ["标题", "序号", "装饰"]),
    ("title-center-sub", "居中副标题", "标题",
     ":::center\n{color:#8a8a8a}{副标题文字}{/color}\n:::\n\n", ["标题", "副标题", "居中"]),
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
    ("divider-dashed-line", "虚线分割", "分割线",
     "\n:::dashed\n:::\n\n", ["分割线", "虚线"]),
    ("divider-gradient", "渐变分割线", "分割线",
     "\n:::gradient\n:::\n\n", ["分割线", "渐变", "装饰"]),
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
    # 图文 (5) — image slots use a rendered example placeholder (WYSIWYG)
    ("image", "单图", "图文", "![{图片说明}](" + EXAMPLE_IMG + ")\n\n", ["图片"]),
    ("image-caption", "图片+说明", "图文",
     "![](" + EXAMPLE_IMG + ")\n:::center\n_{图注文字}_\n:::\n\n", ["图片", "图注"]),
    ("image-text", "左图右文", "图文",
     "![](" + EXAMPLE_IMG + ")\n\n{配图段落文字}\n\n", ["图文"]),
    ("gallery-2", "两图并排", "图文",
     "| ![](" + EXAMPLE_IMG + ") | ![](" + EXAMPLE_IMG + ") |\n|---|---|\n\n", ["图文", "并排"]),
    ("cover-title", "封面图+标题", "图文",
     "![](" + EXAMPLE_IMG + ")\n\n## {文章标题}\n\n", ["图文", "封面"]),
    ("image-card", "图片卡片", "图文",
     ":::imgcard\n![](" + EXAMPLE_IMG + ")\n{图注文字}\n:::\n\n", ["图文", "卡片", "图注", "边框"]),
    ("image-card-plain", "圆角边框图", "图文",
     ":::imgcard\n![](" + EXAMPLE_IMG + ")\n:::\n\n", ["图文", "边框", "圆角"]),
    # 布局 (多栏 — 秀米式并排；:::col 内可放普通 markdown / :::directive) (3)
    ("cols-2", "双栏", "布局",
     "::::columns\n:::col\n{左栏内容}\n:::\n:::col\n{右栏内容}\n:::\n::::\n\n",
     ["布局", "双栏", "并排"]),
    ("cols-3", "三栏", "布局",
     "::::columns\n:::col\n{第一栏}\n:::\n:::col\n{第二栏}\n:::\n"
     ":::col\n{第三栏}\n:::\n::::\n\n", ["布局", "三栏", "并排"]),
    ("cols-image-text", "图文并排", "布局",
     "::::columns 1:2\n:::col\n![](" + EXAMPLE_IMG + ")\n:::\n:::col\n{右侧文字段落}\n:::\n::::\n\n",
     ["布局", "图文", "并排"]),
    # 引用框 (3)
    ("quote-simple", "普通引用", "引用框", "> {引用文字}\n\n", ["引用"]),
    ("quote-author", "引用+出处", "引用框",
     "> {引用文字}\n>\n> —— {出处/作者}\n\n", ["引用", "出处"]),
    ("quote-accent", "强调引用框", "引用框",
     ":::card\n> {重点引用内容}\n:::\n\n", ["引用", "强调"]),
    ("quote-card-large", "大引用卡片", "引用框",
     ":::card\n{color:#4a90d9}❝{/color} {引用文字}\n{color:#999999}—— {出处/作者}{/color}\n:::\n\n",
     ["引用", "卡片"]),
    ("quote-emphasis-bg", "强调色块卡片", "引用框",
     ":::info\n📌 ==重点== {重点强调内容}\n:::\n\n", ["引用", "强调", "色块"]),
    ("quote-emphasis-border", "左边框强调", "引用框",
     ":::highlight\n{color:#07C160}▍{/color} {重点内容}\n:::\n\n", ["引用", "强调", "边框"]),
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
    # ── batch 2: steps / number list / wrapper blocks / variants ──
    # 列表
    ("steps-flow", "步骤流程", "列表",
     ":::steps\n{第一步说明}\n{第二步说明}\n{第三步说明}\n:::\n\n",
     ["列表", "步骤", "流程", "时间线"]),
    ("num-list", "序号列表", "列表",
     ":::numlist\n{第一项}\n{第二项}\n{第三项}\n:::\n\n",
     ["列表", "序号", "圆圈数字"]),
    # 卡片 — wrapper blocks
    ("box-bg", "背景色块", "卡片",
     ":::box\n{要突出的整段内容}\n:::\n\n", ["卡片", "背景", "色块", "容器"]),
    ("box-border", "边框容器", "卡片",
     ":::border\n{要框起来的整段内容}\n:::\n\n", ["卡片", "边框", "容器"]),
    # 按钮 variants
    ("btn-primary", "主要按钮", "按钮",
     ":::button\n阅读原文|{链接URL}\n:::\n\n", ["按钮", "主要"]),
    ("btn-secondary", "次要按钮", "按钮",
     ":::button\n了解更多|{链接URL}\n:::\n\n", ["按钮", "次要"]),
    # 标签 variants
    ("tags-topic", "话题标签", "标签",
     ":::tags\n#{话题一}\n#{话题二}\n#{话题三}\n:::\n\n", ["标签", "话题"]),
    # 页脚 — 关注引导 / 二维码
    ("footer-follow", "关注引导卡片", "页脚",
     ":::card\n:::center\n**关注「{公众号名}」**\n:::\n:::center\n{color:#888888}第一时间获取前沿资讯{/color}\n:::\n:::\n\n",
     ["页脚", "关注", "引导"]),
    ("footer-qrcode", "二维码关注", "页脚",
     ":::card\n:::center\n![公众号二维码]({二维码图片URL})\n:::\n:::center\n**长按识别二维码关注**\n:::\n:::\n\n",
     ["页脚", "二维码", "关注"]),
    # ── style-parameterized components (color= / align=) ──
    ("box-custom-color", "自定义色块", "卡片",
     ":::box color=#fff2e8\n{自定义背景内容}\n:::\n\n", ["卡片", "背景", "自定义颜色"]),
    ("callout-custom-color", "自定义强调框", "卡片",
     ":::tip color=#e74c3c\n{自定义颜色强调内容}\n:::\n\n", ["卡片", "强调", "自定义颜色"]),
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


# Scenario categories for builtin templates (display order for the filter tags).
TEMPLATE_CATEGORIES = ["营销推广", "资讯科技", "教程科普", "节日活动", "职场办公"]
_T_CATEGORIES = {
    "product-launch": "营销推广",
    "review": "营销推广",
    "deep-tech": "资讯科技",
    "news": "资讯科技",
    "interview": "资讯科技",
    "tutorial": "教程科普",
    "list-article": "教程科普",
    "festival": "节日活动",
    "event": "节日活动",
    "recruit": "职场办公",
    "weekly": "职场办公",
}


def builtin_templates() -> list[EditorTemplate]:
    return [EditorTemplate(id=t[0], name=t[1], theme=t[2], markdown=t[3],
                           category=_T_CATEGORIES.get(t[0], "通用"),
                           is_builtin=True) for t in _T]


# ── Materials (icons / decorative snippets) ──────────────────────────────
# 秀米-style material palette — insertable inline glyphs / decorative lines.
# Strictly NO emoji: only typographic & geometric Unicode marks that render as
# plain text glyphs (so they stay black in WeChat, never colored emoji).
_M: list[tuple[str, str, str, str]] = [
    # 图片 — AI 生成的装饰图片素材（bundled under app/web/static/materials,
    # served at /static/materials/*.png）。插入为块级 Markdown 图片。
    ("img-divider-leaf", "枝叶分割线", "图片",
     "![枝叶分割线](/static/materials/material-divider-leaf.png)"),
    ("img-divider-ink", "水墨梅花分割线", "图片",
     "![水墨梅花分割线](/static/materials/material-divider-ink.png)"),
    ("img-title-header", "标题装饰栏", "图片",
     "![标题装饰栏](/static/materials/material-title-header.png)"),
    ("img-badge-seal", "圆形印章框", "图片",
     "![圆形印章框](/static/materials/material-badge-seal.png)"),
    ("img-quote-ornament", "引用装饰引号", "图片",
     "![引用装饰引号](/static/materials/material-quote-ornament.png)"),
    ("img-footer-banner", "关注引导横幅", "图片",
     "![关注引导横幅](/static/materials/material-footer-banner.png)"),
    # 符号 — points / bullets
    ("mark-bar", "竖线", "符号", "▎"),
    ("mark-bar-thin", "细竖线", "符号", "▏"),
    ("mark-dot", "实心圆", "符号", "●"),
    ("mark-dot-o", "空心圆", "符号", "○"),
    ("mark-dot-ring", "圆环", "符号", "◉"),
    ("mark-bullet", "小圆点", "符号", "•"),
    ("mark-diamond", "实心菱形", "符号", "◆"),
    ("mark-diamond-o", "空心菱形", "符号", "◇"),
    ("mark-diamond-d", "宝石菱形", "符号", "◈"),
    ("mark-square", "实心方块", "符号", "■"),
    ("mark-square-o", "空心方块", "符号", "□"),
    ("mark-tri-up", "上三角", "符号", "▲"),
    ("mark-tri-down", "下三角", "符号", "▼"),
    ("mark-tri-right", "右三角", "符号", "▶"),
    ("mark-tri-left", "左三角", "符号", "◀"),
    ("mark-star", "实心星", "符号", "★"),
    ("mark-star-o", "空心星", "符号", "☆"),
    ("mark-star4", "四角星", "符号", "✦"),
    ("mark-flower", "六角花", "符号", "❖"),
    ("mark-ref", "参考星", "符号", "※"),
    ("mark-check", "对勾", "符号", "✓"),
    ("mark-cross", "叉号", "符号", "✕"),
    # 圆圈数字 — numbered badges
    ("num-1", "①", "圆圈数字", "①"),
    ("num-2", "②", "圆圈数字", "②"),
    ("num-3", "③", "圆圈数字", "③"),
    ("num-4", "④", "圆圈数字", "④"),
    ("num-5", "⑤", "圆圈数字", "⑤"),
    ("num-6", "⑥", "圆圈数字", "⑥"),
    ("num-7", "⑦", "圆圈数字", "⑦"),
    ("num-8", "⑧", "圆圈数字", "⑧"),
    ("num-9", "⑨", "圆圈数字", "⑨"),
    ("num-10", "⑩", "圆圈数字", "⑩"),
    # 箭头 — arrows
    ("arr-r", "右箭头", "箭头", "→"),
    ("arr-l", "左箭头", "箭头", "←"),
    ("arr-up", "上箭头", "箭头", "↑"),
    ("arr-down", "下箭头", "箭头", "↓"),
    ("arr-dbl-r", "双线右箭头", "箭头", "⇒"),
    ("arr-tri", "三角箭头", "箭头", "➤"),
    ("arr-thick", "粗右箭头", "箭头", "➔"),
    # 括号 / 引号 — brackets & quotes
    ("br-lenticular", "方头括号", "括号引号", "【】"),
    ("br-tortoise", "六角括号", "括号引号", "〖〗"),
    ("br-corner", "直角引号", "括号引号", "「」"),
    ("br-corner-d", "双直角引号", "括号引号", "『』"),
    ("br-title", "书名号", "括号引号", "《》"),
    ("q-curly-l", "花体左引号", "括号引号", "❝"),
    ("q-curly-r", "花体右引号", "括号引号", "❞"),
    ("q-double", "中文双引号", "括号引号", "“”"),
    # 分隔线 — decorative dividers
    ("deco-dots", "圆点分隔", "分隔线", "· · · · · · · · · ·"),
    ("deco-wave", "波浪分隔", "分隔线", "〜〜〜〜〜〜〜〜〜〜"),
    ("deco-stars", "三星分隔", "分隔线", "✦ ✦ ✦"),
    ("deco-flower", "花饰分隔", "分隔线", "❖ ❖ ❖"),
    ("deco-diamond", "菱形分隔", "分隔线", "◆ ◇ ◆ ◇ ◆"),
    ("deco-square", "方块分隔", "分隔线", "■ □ ■ □ ■"),
    ("deco-mix", "点星分隔", "分隔线", "· ✦ · ✦ · ✦ ·"),
    ("deco-dashline", "点划分隔", "分隔线", "— · — · — · —"),
    ("deco-line", "细实线", "分隔线", "———————————"),
    ("deco-bracket", "书名号分隔", "分隔线", "《 ※ 》"),
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


def get_template(template_id: str,
                 store: "Store | None" = None) -> EditorTemplate | None:
    for t in builtin_templates():
        if t.id == template_id:
            return t
    if store is not None:
        for t in custom_templates(store):
            if t.id == template_id:
                return t
    return None


# ── custom templates (persisted in settings, mirrors custom components) ──────
def _load_custom_templates_raw(store: "Store") -> list[dict]:
    raw = store.get_setting(CUSTOM_TEMPLATES_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _save_custom_templates_raw(store: "Store", items: list[dict]) -> None:
    if items:
        store.set_setting(CUSTOM_TEMPLATES_KEY,
                          json.dumps(items, ensure_ascii=False))
    else:
        store.delete_setting(CUSTOM_TEMPLATES_KEY)


def custom_templates(store: "Store") -> list[EditorTemplate]:
    out: list[EditorTemplate] = []
    for d in _load_custom_templates_raw(store):
        try:
            out.append(EditorTemplate(
                id=str(d["id"]), name=str(d.get("name") or d["id"]),
                markdown=str(d.get("markdown") or ""),
                theme=str(d.get("theme") or "default"),
                category="自定义", is_builtin=False))
        except (KeyError, TypeError):
            continue
    return out


def _builtin_template_ids() -> set[str]:
    return {t[0] for t in _T}


def all_templates(store: "Store | None" = None) -> list[EditorTemplate]:
    tpls = builtin_templates()
    if store is not None:
        tpls += custom_templates(store)
    return tpls


def save_custom_template(store: "Store", *, id: str | None, name: str,
                         markdown: str,
                         theme: str = "default") -> EditorTemplate:
    name = (name or "").strip()
    if not name:
        raise ValueError("模板名称不能为空")
    markdown = markdown or ""
    if not markdown.strip():
        raise ValueError("模板内容不能为空")
    theme = (theme or "default").strip() or "default"

    items = _load_custom_templates_raw(store)
    if id:
        sid = str(id).strip()
        if sid in _builtin_template_ids():
            raise ValueError("不能覆盖内置模板")
        found = False
        for it in items:
            if it.get("id") == sid:
                it.update(name=name, markdown=markdown, theme=theme)
                found = True
                break
        if not found:
            raise ValueError(f"模板不存在：{sid}")
    else:
        base = _slugify(name)
        sid = base
        existing = _builtin_template_ids() | {it.get("id") for it in items}
        i = 2
        while sid in existing:
            sid = f"{base}-{i}"
            i += 1
        items.append({"id": sid, "name": name, "markdown": markdown,
                      "theme": theme})
    _save_custom_templates_raw(store, items)
    return EditorTemplate(id=sid, name=name, markdown=markdown, theme=theme,
                          is_builtin=False)


def delete_custom_template(store: "Store", template_id: str) -> bool:
    sid = str(template_id).strip()
    if sid in _builtin_template_ids():
        raise ValueError("不能删除内置模板")
    items = _load_custom_templates_raw(store)
    new_items = [it for it in items if it.get("id") != sid]
    if len(new_items) == len(items):
        return False
    _save_custom_templates_raw(store, new_items)
    return True
