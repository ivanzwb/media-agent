"""Read what the user asked for before searching for it.

The topic box takes whatever the user types: a bare term, a spoken-language
request, a question, sometimes just a URL they pasted. All of it used to go
to the search engine as-is (only punctuation and question tails stripped),
which searches for the sentence rather than the subject — and a pasted URL
searched for the URL.

So the subject is worked out first, then the queries are written against that
understanding. Both come back from one model call, with the understanding
fields ahead of the queries so they are written first and the queries follow
from them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import re

from app.llm.base import LLMProvider, Message
from app.pipeline.rewriter import _extract_json

logger = logging.getLogger(__name__)

# The understanding travels into later prompts, so a model that answers with an
# essay would push out what those prompts are actually about.
_SUBJECT_CHARS = 80
_GOAL_CHARS = 200
_AUDIENCE_CHARS = 40
_FOCUS_CHARS = 40
_MAX_FOCUS = 5
_MAX_QUERIES = 6

_ZH_QUESTION_TAIL = re.compile(
    r"(?:好吗|好不好|行不行|对不对|可以吗|能不能|是不是|会不会|有没有"
    r"|怎么样|怎么办|为什么|是否|如何)$")
_ZH_MODAL_WORDS = re.compile(r"真的|到底|究竟|其实|确实|应该")


def topic_search_terms(topic: str, lang: str) -> str:
    """Reduce a conversational topic to search terms without a model.

    Search engines match natural-language questions poorly, so punctuation
    becomes term separators and language-level question/modal words are
    dropped. Only function words are removed; subject matter is preserved.

    This is the offline floor: it cannot tell a subject from a request, which
    is what :func:`understand_topic` is for.
    """
    compact = " ".join(re.sub(r"[,，。、！？!?；;：:]+", " ", topic).split())
    if lang == "en":
        return compact
    segments = []
    for segment in compact.split():
        segment = _ZH_MODAL_WORDS.sub("", segment)
        segment = _ZH_QUESTION_TAIL.sub("", segment)
        if segment:
            segments.append(segment)
    return " ".join(segments) or compact or topic


def _local_query_fallback(base: str) -> list[str]:
    """Bilingual queries built without the model, for when understanding fails."""
    return [base, f"{base} 分析", f"{base} 专家 建议",
            f"{base} research", f"{base} expert analysis"]


@dataclass(frozen=True)
class TopicIntent:
    """What the user is after, and the queries that follow from it.

    ``understood`` is False when the model could not be reached or answered
    with something unusable. The object is still complete and usable then —
    every field falls back to what can be derived offline — so callers never
    have to handle a missing intent, only a shallower one.
    """

    raw: str
    subject: str
    goal: str = ""
    audience: str = ""
    focus: tuple[str, ...] = ()
    queries: tuple[str, ...] = field(default_factory=tuple)
    understood: bool = False

    @classmethod
    def fallback(cls, topic: str, lang: str) -> "TopicIntent":
        base = topic_search_terms(topic, lang)
        return cls(raw=topic, subject=base,
                   queries=tuple(_local_query_fallback(base)))

    def summary(self) -> str:
        """One line, for the progress feed and the logs."""
        return f"{self.subject}（{self.goal}）" if self.goal else self.subject

    def brief(self) -> str:
        """The understanding as a prompt block, empty when there is none.

        Only the model's own reading is worth passing on: the offline fallback
        knows nothing the receiving prompt does not already have.
        """
        if not self.understood:
            return ""
        lines = [f"- 要讲的主题：{self.subject}"]
        if self.goal:
            lines.append(f"- 用户想要的：{self.goal}")
        if self.audience:
            lines.append(f"- 读者：{self.audience}")
        if self.focus:
            lines.append(f"- 用户点明的重点：{'、'.join(self.focus)}")
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {
            "subject": self.subject,
            "goal": self.goal,
            "audience": self.audience,
            "focus": list(self.focus),
            "understood": self.understood,
        }


_SYSTEM = (
    "你是中文内容选题编辑。用户填在选题框里的，可能是一个词、一句口语化的要求、"
    "一个问题，也可能只是一段网址。你的活儿分两步：先弄清楚他想要一篇什么样的"
    "文章，再按这个理解给出检索词。"
)

_KINDS = {
    "article": "一篇文章",
    "series": "一个成系列的连载",
}


def _prompt(topic: str, kind: str) -> str:
    return (
        f"用户想做{_KINDS.get(kind, _KINDS['article'])}，选题框里填的是：\n"
        f"{topic}\n\n"
        "第一步，读懂这段输入：\n"
        "- subject：真正要讲的主题，一个短语，别照抄原话，也别写成问句。"
        "输入是网址时，从域名、路径和项目名判断它讲的是什么东西。\n"
        "- goal：用户想要的是什么，一句话。比如想弄懂某个概念、想在几个方案里"
        "做选择、想知道值不值得上手、想解决手上一个具体麻烦。\n"
        "- audience：写给谁看，看不出来就留空字符串，不要硬猜。\n"
        "- focus：用户自己点明的重点或限定条件，逐条抄下来；没有就给空数组。\n"
        "第二步，按第一步的理解写 4-6 个适合查找高质量新闻、研究和深度文章的"
        "搜索引擎检索词。中文和英文检索词都要有，数量大致各半；英文用该领域"
        "英文资料里实际通用的说法，不要把中文逐字翻译过去。检索词要短而具体，"
        "把最有区分度的核心概念放在最前面，不要照抄完整问句；"
        "同时覆盖专业解释、风险/收益、专家建议和可靠数据。\n\n"
        "只输出 JSON，字段顺序照给：\n"
        '{"subject":"...","goal":"...","audience":"...",'
        '"focus":["..."],"queries":["..."]}'
    )


def _text(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def understand_topic(topic: str, lang: str, provider: LLMProvider, *,
                     kind: str = "article") -> TopicIntent:
    """Work out what the user wants written, and what to search for.

    Never raises and never returns None: a topic that cannot be understood
    falls back to :meth:`TopicIntent.fallback`, because a shallower search is
    a far better outcome than a failed run.
    """
    topic = (topic or "").strip()
    fallback = TopicIntent.fallback(topic, lang)
    if not topic:
        return fallback
    try:
        raw = provider.chat([
            Message(role="system", content=_SYSTEM),
            Message(role="user", content=_prompt(topic, kind)),
        ])
        parsed = _extract_json(raw)
        if not isinstance(parsed, dict):
            logger.warning("选题理解未返回 JSON 对象，使用本地兜底；响应：%r",
                           str(raw)[:300])
            return fallback

        queries = [_text(query, _SUBJECT_CHARS)
                   for query in parsed.get("queries") or []]
        queries = [query for query in queries if query]
        subject = _text(parsed.get("subject"), _SUBJECT_CHARS)
        if not subject and not queries:
            logger.warning("选题理解既没给 subject 也没给 queries，使用本地兜底；"
                           "响应：%r", str(raw)[:300])
            return fallback

        # A model that read the input but skipped `subject` still understood
        # more than the regex floor does — keep its queries, derive the subject.
        subject = subject or topic_search_terms(topic, lang)
        raw_focus = parsed.get("focus") or []
        if not isinstance(raw_focus, list):
            raw_focus = [raw_focus]
        focus = [_text(item, _FOCUS_CHARS) for item in raw_focus[:_MAX_FOCUS]]
        # The subject leads: it is the most searchable form of the topic, and
        # every downstream ranking is scored against it.
        ordered = list(dict.fromkeys([subject, *queries]))[:_MAX_QUERIES]
        return TopicIntent(
            raw=topic,
            subject=subject,
            goal=_text(parsed.get("goal"), _GOAL_CHARS),
            audience=_text(parsed.get("audience"), _AUDIENCE_CHARS),
            focus=tuple(item for item in focus if item),
            queries=tuple(ordered),
            understood=True,
        )
    except Exception as exc:                            # noqa: BLE001
        logger.warning("选题理解失败，使用本地兜底：%s", exc)
        return fallback
