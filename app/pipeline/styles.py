"""Rewrite styles (转写风格) — a configurable, switchable rewriting persona.

A *style* bundles a system prompt (the LLM's role/tone) and a user
instruction template.  Templates use four placeholders, substituted at
render time by :func:`app.pipeline.rewriter.render_instruction`:

    {title}     original article title
    {source}    original source name
    {manifest}  media manifest describing available images/videos
    {content}   original article body (markdown)

Builtin styles ship with the app.  Users can create/edit/delete their own
styles, persisted as JSON in the ``settings`` table under the key
``rewrite_custom_styles``.

Backward compatibility: the default style ``deep-tech`` reproduces the
original hardcoded behaviour (深度科技报道) exactly, so nothing changes for
existing users who never pick a style.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.pipeline.anti_slop import ANTI_SLOP_SYSTEM_INSTRUCTION
from app.pipeline.rewriter import REWRITE_INSTRUCTION, REWRITE_SYSTEM

if TYPE_CHECKING:
    from app.store import Store

CUSTOM_STYLES_KEY = "rewrite_custom_styles"
DEFAULT_STYLE_KEY = "rewrite_style"
DEFAULT_STYLE_ID = "deep-tech"


@dataclass
class RewriteStyle:
    id: str
    name: str
    description: str
    prompt: str                 # system prompt (role/tone)
    instruction: str            # user instruction template (single-brace literals)
    is_builtin: bool = False
    is_default: bool = False

    def to_public(self) -> dict:
        """Serialise for the API / templates."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "prompt": self.prompt,
            "instruction": self.instruction,
            "is_builtin": self.is_builtin,
            "is_default": self.is_default,
        }


# ── Shared instruction scaffolding for the non-default styles ────────────
# The original 深度科技报道 instruction is bespoke; every other builtin shares
# a common skeleton (media + output rules) and only varies the "写作风格"
# guidance block, which keeps media handling and JSON output identical while
# letting the tone differ.

_COMMON_RULES = (
    "### 通用要求（所有风格通用）\n"
    "- 基于且仅基于原文事实改写，绝不编造原文没有的数据、引用、结论或时间；不确定的不要写。\n"
    "- 数据全部保留，这是硬价值。专业术语首次出现时用通俗语言解释，可括号标注英文。\n"
    "- 短句、短段落，手机友好。长难句拆成 2-3 个短句。\n"
    "- 结尾注明原文标题、来源，以及（若原文提供）日期。\n"
    "{promotion_block}\n\n"
    + ANTI_SLOP_SYSTEM_INSTRUCTION + "\n\n"
)

_MEDIA_AND_OUTPUT = (
    "### 图文结合\n{manifest}\n\n"
    "### 图片/视频插入规则\n"
    "- 原文图片已用 `![](url)` 嵌入正文，改写时把它们保留在相关内容附近，"
    "不要全部堆到文末。\n"
    "- 保留最相关的 2-4 张图片；若有原文视频，用 [[VID:0]] 占位符插在最相关位置。\n"
    "- 与内容不相关的图/视频删掉，不要硬塞，也不要编造不存在的媒体。\n\n"
    "### 输出格式（严格）\n"
    "输出严格的 JSON，字段：\n"
    '  "title_candidates": [3 个吸睛但不虚假的标题],\n'
    '  "body_md": "Markdown 正文（图片用 ![](url) 保留、视频用 [[VID:0]] 插入）"\n'
    "⚠️ 关键：你的回复必须从 { 开始、以 } 结束，不要加任何前缀、后缀、解释、"
    "摘要或代码围栏。body_md 字段中的换行必须用 \\n 转义，不能出现真正的换行符。"
    "不要写“已完成”“已输出至文件”等元评论。\n\n"
    "原文标题：{title}\n来源：{source}\n\n原文正文：\n{content}"
)


def _build_instruction(style_guidance: str) -> str:
    return (
        "分两步完成：\n\n"
        "**第一步 — 完整理解**：通读全文，确保理解所有技术细节、数据、结论和引用。\n\n"
        "**第二步 — 按指定风格改写**：在理解的基础上，写成中文文章。\n\n"
        "### 写作风格\n" + style_guidance.strip() + "\n\n"
        + _COMMON_RULES + _MEDIA_AND_OUTPUT
    )


def _deep_tech_instruction() -> str:
    """The original REWRITE_INSTRUCTION converted from str.format form
    (``{{``/``}}`` escaped braces) into the replace-based single-brace form
    used by :func:`render_instruction`.  The rendered output is identical to
    the previous ``.format`` behaviour."""
    return REWRITE_INSTRUCTION.replace("{{", "{").replace("}}", "}")


# ── Builtin style definitions ────────────────────────────────────────────
# Each spec: (id, name, description, system_prompt, style_guidance)
_BUILTIN_SPECS: list[tuple[str, str, str, str, str]] = [
    (
        "silicon-valley-memo", "硅谷内部信",
        "像科技公司 CEO 内部信，冷静坦诚、一针见血，只说本质、信息密度高",
        "你是一位科技公司创始人 / CEO，用内部信的口吻向团队坦诚地讲清一件事。"
        "冷静、克制、一针见血，只说本质，不渲染情绪。",
        "像科技公司 CEO 写给全员的内部信：冷静坦诚、直击本质。"
        "段落简短，信息密度高，不绕弯子。开门见山点出「这件事为什么重要」，"
        "然后层层拆解，最后给出判断或行动含义。不用夸张形容词，不喊口号。",
    ),
    (
        "zhihu-answer", "知乎高赞回答",
        "「先说结论」结构，第一人称、有判断、专业但不端着，像行业老兵答题",
        "你是某领域深耕十年的老兵，在知乎认真答题。专业但不端着，有第一人称的"
        "思考和判断，偶尔带点克制的幽默。",
        "用知乎高赞回答的结构：以「先说结论：……」开头，再展开论证。"
        "第一人称、有观点、有判断，专业但口语化，适当用「其实」「说白了」这类"
        "过渡。可以有一两处克制的幽默，但不喧宾夺主。",
    ),
    (
        "economist", "经济学人",
        "冷静克制的英式分析，长句严谨、数据说话，像《经济学人》科技季刊",
        "你是《经济学人》风格的资深撰稿人，冷静克制、逻辑严密，用数据和事实说话。",
        "英式冷静分析风格：长句严谨、逻辑缜密，拒绝情绪化和夸张。"
        "每段围绕一个论点展开，用数据支撑。整体像《经济学人》的 Technology "
        "Quarterly——理性、审慎、有全局视角。",
    ),
    (
        "standup", "脱口秀",
        "用段子和吐槽讲科技，有梗有态度，笑着看完还能记住要点",
        "你是一位懂科技的脱口秀演员，用段子和吐槽把硬核技术讲得又好笑又好懂。",
        "用脱口秀的节奏讲科技：有梗、有吐槽、有态度。把信息点藏在段子里，"
        "让读者笑着看完还能记住要点。可以自嘲、可以调侃行业现象，"
        "但不歪曲事实、不低俗。参考黄西 / Jimmy O. Yang 的观察式幽默。",
    ),
    (
        "sci-fi-codex", "科幻设定集",
        "把真实技术写成科幻「未来史」，冷静叙述不可思议之事，参考《三体》设定章节",
        "你是一位硬科幻作家，把已经发生的真实技术，用「未来史」的冷静口吻叙述。",
        "把技术写成科幻小说的设定集：用冷静、略带疏离的口吻，"
        "像在描述一个不可思议但已成真的世界。参考《三体》的「背景设定」章节——"
        "克制地陈述惊人之事，让技术本身的震撼自己说话。不得虚构原文没有的情节。",
    ),
    (
        "brief", "简报·晚点",
        "极简克制，短句短段、零废话、信息密度极高，适合快速阅读",
        "你是《晚点 LatePost》的简报撰稿人，极简克制，零废话。",
        "极简克制的内部简报风格：短句、短段，每个自然段不超过两行。"
        "信息密度极高，去掉所有修饰和废话，只留事实与判断。适合快速阅读。",
    ),
    (
        "sharp-take", "虎嗅锐评",
        "锐利批判，直指商业本质，每篇有明确判断，适合公司战略/行业分析",
        "你是一位犀利的商业科技评论人，敢于质疑、直指利益动机和商业本质。",
        "锐利、有批判性的评论风格：不回避利益动机和商业本质，敢于提出质疑。"
        "全文要有一个明确的「判断」或立场，并用原文事实支撑。"
        "适合分析公司战略、行业动态。但立场必须建立在事实上，不得情绪化捏造。",
    ),
    (
        "documentary", "纪录片旁白",
        "沉稳大气的旁白体，画面感强、有节奏，特别适合视频脚本",
        "你是 BBC / CCTV 纪录片的解说撰稿人，沉稳大气、画面感强。",
        "沉稳、大气的纪录片旁白体：像 BBC / CCTV 纪录片解说词。"
        "画面感强、有节奏感，善用停顿式短句营造氛围。特别适合作为视频脚本。"
        "叙述宏观趋势时保持克制与庄重。",
    ),
    (
        "letter-to-friend", "给朋友写信",
        "口语亲切的书信体，像朋友聊天一样自然，适合 Newsletter",
        "你在给一位好友写信，分享今天读到的有意思的东西。亲切、口语、真诚。",
        "口语化、亲切的书信体：像「嘿，今天读到个挺有意思的东西……」这样开头。"
        "不追求结构完整，追求像朋友聊天一样自然流畅。可以有个人化的语气，"
        "但事实要准确。适合 Newsletter。",
    ),
    (
        "product-review", "产品评测",
        "第一人称评测，先给结论再讲优缺点，有场景有对比有建议",
        "你是一位第一人称的产品评测人，先给结论再讲理由，有场景、有对比、有建议。",
        "第一人称的产品评测风格：开头直接给结论（推荐 / 不推荐 / 看情况），"
        "然后展开优缺点。多用具体场景和对比，结尾给出明确的适用人群或建议。"
        "参考 The Verge / 钟文泽 的评测风格。所有结论都要基于原文事实。",
    ),
]


def builtin_styles() -> list[RewriteStyle]:
    """Return the ordered list of builtin styles (default first)."""
    styles: list[RewriteStyle] = [
        RewriteStyle(
            id=DEFAULT_STYLE_ID,
            name="深度科技报道",
            description="像《极客公园》《硅星人》的中文深度技术报道，故事化叙述 + 中文数字编号章节",
            prompt=REWRITE_SYSTEM,
            instruction=_deep_tech_instruction(),
            is_builtin=True,
            is_default=True,
        )
    ]
    for sid, name, desc, system, guidance in _BUILTIN_SPECS:
        styles.append(RewriteStyle(
            id=sid, name=name, description=desc,
            prompt=system,
            instruction=_build_instruction(guidance),
            is_builtin=True,
        ))
    return styles


# ── Custom (user-defined) styles ─────────────────────────────────────────

def _load_custom_raw(store: "Store") -> list[dict]:
    raw = store.get_setting(CUSTOM_STYLES_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


def custom_styles(store: "Store") -> list[RewriteStyle]:
    out: list[RewriteStyle] = []
    for d in _load_custom_raw(store):
        try:
            out.append(RewriteStyle(
                id=str(d["id"]), name=str(d.get("name") or d["id"]),
                description=str(d.get("description") or ""),
                prompt=str(d.get("prompt") or ""),
                instruction=str(d.get("instruction") or ""),
                is_builtin=False,
            ))
        except (KeyError, TypeError):
            continue
    return out


def _builtin_ids() -> set[str]:
    return {DEFAULT_STYLE_ID} | {s[0] for s in _BUILTIN_SPECS}


def all_styles(store: "Store | None" = None) -> list[RewriteStyle]:
    """All available styles: builtins first, then user custom styles.

    If *store* is provided, the ``is_default`` flag is recomputed from the
    persisted default-style setting.
    """
    styles = builtin_styles()
    if store is not None:
        styles += custom_styles(store)
        default_id = get_default_style_id(store)
        for s in styles:
            s.is_default = (s.id == default_id)
    return styles


def get_style(style_id: str | None, store: "Store | None" = None) -> RewriteStyle | None:
    if not style_id:
        return None
    for s in all_styles(store):
        if s.id == style_id:
            return s
    return None


def get_default_style_id(store: "Store | None") -> str:
    """The configured global default style id, falling back to deep-tech.

    Checks ids directly (not via :func:`all_styles`) to avoid recursion.
    """
    if store is not None:
        val = store.get_setting(DEFAULT_STYLE_KEY)
        if val:
            valid = _builtin_ids() | {s.id for s in custom_styles(store)}
            if val in valid:
                return val
    return DEFAULT_STYLE_ID


def resolve_style(style_id: str | None, store: "Store | None") -> RewriteStyle:
    """Resolve *style_id* (may be None) to a concrete style, always returning
    a valid style — the configured default, else deep-tech."""
    s = get_style(style_id, store) if style_id else None
    if s is not None:
        return s
    default_id = get_default_style_id(store)
    s = get_style(default_id, store)
    if s is not None:
        return s
    # Last-resort: the hardcoded deep-tech builtin.
    return builtin_styles()[0]


# ── Custom style CRUD ────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "custom"


def _save_custom_raw(store: "Store", items: list[dict]) -> None:
    if items:
        store.set_setting(CUSTOM_STYLES_KEY, json.dumps(items, ensure_ascii=False))
    else:
        store.delete_setting(CUSTOM_STYLES_KEY)


def save_custom_style(store: "Store", *, id: str | None, name: str,
                      description: str, prompt: str,
                      instruction: str) -> RewriteStyle:
    """Create or update a custom style. Raises ValueError on invalid input
    or id collision with a builtin."""
    name = (name or "").strip()
    if not name:
        raise ValueError("风格名称不能为空")
    prompt = (prompt or "").strip()
    instruction = (instruction or "").strip()
    if "{content}" not in instruction:
        raise ValueError("指令模板必须包含 {content} 占位符")

    items = _load_custom_raw(store)
    if id:
        sid = str(id).strip()
        if sid in _builtin_ids():
            raise ValueError("不能覆盖内置风格")
        found = False
        for it in items:
            if it.get("id") == sid:
                it.update(name=name, description=description.strip(),
                          prompt=prompt, instruction=instruction)
                found = True
                break
        if not found:
            raise ValueError(f"风格不存在：{sid}")
    else:
        # New style: derive a unique id from the name.
        base = _slugify(name)
        sid = base
        existing = _builtin_ids() | {it.get("id") for it in items}
        i = 2
        while sid in existing:
            sid = f"{base}-{i}"
            i += 1
        items.append({"id": sid, "name": name,
                      "description": description.strip(),
                      "prompt": prompt, "instruction": instruction})
    _save_custom_raw(store, items)
    return RewriteStyle(id=sid, name=name, description=description.strip(),
                        prompt=prompt, instruction=instruction, is_builtin=False)


def delete_custom_style(store: "Store", style_id: str) -> bool:
    sid = str(style_id).strip()
    if sid in _builtin_ids():
        raise ValueError("不能删除内置风格")
    items = _load_custom_raw(store)
    new_items = [it for it in items if it.get("id") != sid]
    if len(new_items) == len(items):
        return False
    _save_custom_raw(store, new_items)
    # If the deleted style was the global default, reset to deep-tech.
    if store.get_setting(DEFAULT_STYLE_KEY) == sid:
        store.delete_setting(DEFAULT_STYLE_KEY)
    return True


def set_default_style(store: "Store", style_id: str) -> None:
    sid = (style_id or "").strip()
    if not sid or sid == DEFAULT_STYLE_ID:
        store.delete_setting(DEFAULT_STYLE_KEY)
        return
    if get_style(sid, store) is None:
        raise ValueError(f"风格不存在：{sid}")
    store.set_setting(DEFAULT_STYLE_KEY, sid)
