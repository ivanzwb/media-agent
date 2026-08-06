"""AI 味体检：给文章的机器口吻打个分。

`anti_slop` 那套词表是用来预防的——注进系统提示词，再在改写完正则清一遍。
可它管不到人自己写的稿、外部导入的稿，也管不到模型换了口味之后漏出来的新
花样，更从不告诉你是哪一句出了问题。这里把同一批特征反过来用：只读不改，
命中什么、在第几行、扣了多少分，全摊开给人看。

判分只认聚集，不认单点。一个「此外」说明不了任何事，一篇两千字里塞了八个
「此外」、三处「不仅是……更是……」、结尾还要升华一把，那才是 AI 味。所以
每个维度先按篇幅折算成密度，再拿密度去吃各自的分值上限，孤零零一处命中只
会换来很小的一点分。

体检只报告，不改稿，也不拦发布。
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Pattern


@dataclass(frozen=True)
class _Dimension:
    """一类 AI 腔特征。

    ``weight`` 是这个维度最多能扣的分，``saturate`` 是「每千字命中几次就扣满」。
    两个数字都是经验值，松紧不合适就调它们。
    """

    key: str
    label: str
    weight: float
    saturate: float
    advice: str
    patterns: tuple[Pattern[str], ...] = ()


def _p(*sources: str) -> tuple[Pattern[str], ...]:
    return tuple(re.compile(s) for s in sources)


# 篇幅太短时密度会失真：三十个字里出现一个「此外」，折算成每千字三十次，
# 荒唐。统一按至少 800 字计——这也保证了短稿里孤零零一处命中扣不了多少分。
_MIN_CHARS = 800
# 同一个维度最多列这么多条，剩下的只报个数，免得面板被刷屏。
_MAX_FINDINGS = 12
# 孤例不是证据：一个维度只命中一次时，最多扣到它分值的这个比例。写作里谁都
# 会偶尔冒一句套话，反复出现才说明是机器的口癖。
_LONE_HIT_CAP = 0.3

_DIMENSIONS: tuple[_Dimension, ...] = (
    _Dimension(
        key="opening", label="空泛开头", weight=12, saturate=1.6,
        advice="删掉铺垫，第一句直接说事。",
        patterns=_p(
            r"随着[^。！？\n]{0,30}的(?:发展|到来|普及|推进|深入|兴起|崛起|加速|变革)",
            r"近年来[，,]?",
            r"在当今[^。！？\n]{0,15}",
            r"在[^。！？\n]{1,20}(?:的背景下|背景下|的浪潮中|的大潮中|的今天)",
            r"众所周知[，,]",
            r"不可否认[，,]",
        ),
    ),
    _Dimension(
        key="fake_depth", label="假深刻", weight=14, saturate=1.4,
        advice="这类对仗句读着像有洞见，其实没给新信息，直接说结论。",
        patterns=_p(
            r"不(?:仅仅|仅|只)是[^，。！？\n]{1,40}[，,]\s*(?:更|而)是",
            r"表面上[^，。！？\n]{1,40}[，,]\s*(?:实际上|本质上|背后|骨子里)",
            r"某种(?:意义|程度)上",
            r"真正的[^，。！？\n]{1,20}(?:不是|从来不是|从来都不是)[^，。！？\n]{1,30}[，,]\s*而是",
            r"与其说[^，。！？\n]{1,30}[，,]\s*不如说",
            r"归根(?:结底|到底)",
            r"本质上(?:来说|来讲)?[，,]",
        ),
    ),
    _Dimension(
        key="jargon", label="报告套话", weight=12, saturate=2.5,
        advice="换成具体的人、事、数字，这些词不承载信息。",
        # 「护城河」「价值链」「生态位」这些是正经的商业分析词，人写稿也照用，
        # 放进来只会冤枉人。留下的是离了公文就没人说的那批。
        patterns=_p(
            r"赋能|抓手|(?:形成)?闭环|颗粒度|底层逻辑|顶层设计|扎实推进"
            r"|全链路|降本增效|持续发力|精准发力|深度融合|(?:经验|能力)沉淀",
        ),
    ),
    _Dimension(
        key="techblog", label="科技博客体", weight=10, saturate=1.5,
        advice="这些是流量套路词，删掉不影响读者理解。",
        patterns=_p(
            r"深入浅出|最佳实践|干货(?:满满)?|保姆级|一站式|不容错过"
            r"|值得(?:收藏|一读)|建议收藏|一文(?:读懂|看懂|搞懂|讲透)"
            r"|手把手|划重点|全网最|必看|收藏起来",
        ),
    ),
    _Dimension(
        key="wechat", label="公众号腔", weight=10, saturate=1.0,
        advice="删掉，或者换成一句你真会对朋友说的话。",
        patterns=_p(
            r"愿我们(?:都能)?|这才是真正的|扎心了?|与君共勉|你我共勉"
            r"|写在最后|(?:点个|点)在看|转发给(?:需要的)?",
        ),
    ),
    _Dimension(
        key="fake_oral", label="假口语", weight=8, saturate=1.8,
        advice="装出来的口语比书面语更假，去掉这层伪装。",
        patterns=_p(
            r"说实话|老实说|不得不说|你会发现|其实很简单"
            r"|懂的都懂|你品[，,]?(?:你)?细品|敲黑板|注意了[，,]",
        ),
    ),
    _Dimension(
        key="mechanical", label="机械结构", weight=12, saturate=2.2,
        advice="凑数的框架句，删掉后段落照样成立。",
        patterns=_p(
            r"总的来说|综上所述|总而言之|由此可见|不难看出"
            r"|值得(?:注意|一提)的是|需要注意的是|以下几(?:点|个方面)"
            r"|(?:三|四|五|六)大(?:方面|优势|特点|趋势|原因|挑战)",
            r"首先[^。！？\n]{0,40}[。！？][^。！？\n]{0,60}其次",
        ),
    ),
    _Dimension(
        key="elevation", label="结尾升华", weight=10, saturate=0.8,
        advice="空洞的拔高，读者只会划走，写清楚接下来会发生什么更好。",
        patterns=_p(
            r"在[^。！？\n]{1,25}的(?:道路|路|征程)上",
            r"未来可期|拭目以待|路虽远[，,]?行则将至|时代的(?:洪流|浪潮)"
            r"|让我们(?:一起)?(?:期待|见证)",
        ),
    ),
    _Dimension(
        key="triad", label="三连排比", weight=6, saturate=3.0,
        advice="为了凑齐三项而写的排比，留下真正有内容的那一两项就够。",
        patterns=_p(
            r"[^，。！？、\n]{4,12}、[^，。！？、\n]{4,12}、[^，。！？、\n]{4,12}",
            r"既[^，。！？\n]{2,20}[，,]?又[^，。！？\n]{2,20}[，,]?(?:还|更)",
            r"不仅[^，。！？\n]{2,20}[，,]?(?:还|而且)[^，。！？\n]{2,20}[，,]?更",
        ),
    ),
    _Dimension(
        key="connector", label="连接词堆砌", weight=8, saturate=8.0,
        advice="句子之间的关系靠内容体现，不用每句都挂一个连接词。",
        patterns=_p(
            r"此外[，,]|另外[，,]|与此同时|除此之外|不仅如此"
            r"|更重要的是|换句话说|也就是说|因此[，,]|然而[，,]",
        ),
    ),
    _Dimension(
        key="bold", label="加粗滥用", weight=8, saturate=8.0,
        advice="满屏加粗等于没有重点，一段最多留一处。",
        patterns=_p(r"\*\*[^*\n]{1,40}\*\*"),
    ),
    _Dimension(
        key="emoji", label="emoji 装饰", weight=6, saturate=2.5,
        advice="标题和列表前挂 emoji 是模型的习惯，不是人的习惯。",
        patterns=_p(
            r"^\s*(?:#{1,6}\s*|[-*+]\s+|\d+[.、]\s*)"
            r"[\U0001F300-\U0001FAFF\U0001F000-\U0001F2FF\u2600-\u27BF\u2B00-\u2BFF]",
        ),
    ),
    _Dimension(
        key="dash", label="破折号滥用", weight=6, saturate=4.0,
        advice="多数破折号可以换成句号或逗号，句子会更利落。",
        # 中文破折号是两个 em dash，但单个 em dash 和 en dash 一样是那股味道。
        patterns=_p(r"[—–]+"),
    ),
    _Dimension(
        key="hollow_adj", label="空洞形容词", weight=5, saturate=8.0,
        advice="形容词撑不起分量，换成具体的事实或数字。",
        patterns=_p(
            r"深刻|深远|重大|极致|卓越|显著|巨大|前所未有|无疑|尤为",
        ),
    ),
)

# 句子长短的变异系数（标准差 / 平均值）。人写东西长短句混着来，起伏大；
# 模型倾向于一路输出差不多长的句子。低于 _RHYTHM_FLAT 算齐得反常，
# 到了 _RHYTHM_OK 就完全正常，中间按比例扣分。
_RHYTHM_FLAT = 0.30
_RHYTHM_OK = 0.62
_RHYTHM_WEIGHT = 10.0
# 句子太少时方差没有意义。
_RHYTHM_MIN_SENTENCES = 8

_RHYTHM = _Dimension(
    key="rhythm", label="句子长短太齐", weight=_RHYTHM_WEIGHT, saturate=1.0,
    advice="有意识地穿插短句。一句话说完就断，别都拉成一样长。",
)

_FENCE = re.compile(r"^\s*(?:```|~~~)")
# ::: 是本项目的组件语法，不是正文。
_DIRECTIVE = re.compile(r"^\s*:::")
_URL = re.compile(r"https?://\S+")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_TAG = re.compile(r"<[^>]{1,200}>")
# 参考文献是流水线自己拼的，不是谁写的句子。合成器给每条都配了一个破折号
# （「标题 —— 来源」），照单全收的话，每篇带引用的稿子都要白白背上一笔
# 「破折号滥用」，还会收到「把破折号换成句号」这种对着机器生成的清单说的
# 建议。它永远在正文最后，扫到就收工。
_REFERENCES = re.compile(r"^\s*#{1,6}\s*(?:参考文献|参考资料|References)\s*$",
                         re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"[。！？!?；;]+")
# 统计句长时，标记本身不算字。
_MD_MARK = re.compile(r"[*_`>#\[\]()~|-]+")


def _prose_lines(text: str) -> list[tuple[int, str]]:
    """正文里真正是「话」的那些行，带原始行号。

    代码块、组件标记、图片、链接地址和文末的参考文献都不是人写给读者看的
    句子，留着只会让词表误命中。
    """
    out: list[tuple[int, str]] = []
    in_fence = False
    for index, raw in enumerate((text or "").splitlines()):
        if _FENCE.match(raw):
            in_fence = not in_fence
            continue
        if not in_fence and _REFERENCES.match(raw):
            break
        if in_fence or _DIRECTIVE.match(raw):
            continue
        line = _URL.sub(" ", _IMAGE.sub(" ", raw))
        out.append((index + 1, _HTML_TAG.sub(" ", line)))
    return out


def _sentence_lengths(lines: list[tuple[int, str]]) -> list[int]:
    plain = _MD_MARK.sub("", "\n".join(line for _, line in lines))
    lengths = [len(s.strip()) for s in _SENTENCE_SPLIT.split(plain)]
    # 一两个字的碎片是标点切出来的渣，不是句子。
    return [n for n in lengths if n >= 4]


def _rhythm_points(lengths: list[int]) -> tuple[float, float | None]:
    """句长起伏该扣多少分，以及变异系数本身（句子太少时为 None）。"""
    if len(lengths) < _RHYTHM_MIN_SENTENCES:
        return 0.0, None
    mean = statistics.fmean(lengths)
    if mean <= 0:
        return 0.0, None
    cv = statistics.pstdev(lengths) / mean
    if cv >= _RHYTHM_OK:
        return 0.0, cv
    ratio = (_RHYTHM_OK - max(cv, _RHYTHM_FLAT)) / (_RHYTHM_OK - _RHYTHM_FLAT)
    return _RHYTHM_WEIGHT * ratio, cv


def _level(score: float) -> str:
    if score < 20:
        return "很淡"
    if score < 45:
        return "有一点"
    if score < 70:
        return "偏重"
    return "很重"


def check_text(text: str) -> dict:
    """给 *text* 的 AI 口吻打分，并指出每一处命中在第几行。

    返回 ``score``（0-100，越高越像机器写的）、``level``、逐维度的扣分明细，
    以及一份按行号排好的命中清单。
    """
    lines = _prose_lines(text)
    prose = "\n".join(line for _, line in lines)
    chars = len(re.sub(r"\s", "", _MD_MARK.sub("", prose)))
    per_1000 = 1000.0 / max(chars, _MIN_CHARS)

    dimensions: list[dict] = []
    findings: list[dict] = []
    score = 0.0

    for dim in _DIMENSIONS:
        hits: list[dict] = []
        for line_no, line in lines:
            for pattern in dim.patterns:
                for match in pattern.finditer(line):
                    hits.append({"key": dim.key, "label": dim.label,
                                 "text": match.group(0).strip()[:60],
                                 "line": line_no})
        if not hits:
            continue
        points = dim.weight * min(1.0, len(hits) * per_1000 / dim.saturate)
        if len(hits) == 1:
            points = min(points, dim.weight * _LONE_HIT_CAP)
        score += points
        findings += hits[:_MAX_FINDINGS]
        dimensions.append({
            "key": dim.key, "label": dim.label, "hits": len(hits),
            "points": round(points, 1), "weight": dim.weight,
            "advice": dim.advice, "shown": min(len(hits), _MAX_FINDINGS),
        })

    rhythm_points, cv = _rhythm_points(_sentence_lengths(lines))
    if rhythm_points > 0:
        score += rhythm_points
        dimensions.append({
            "key": _RHYTHM.key, "label": _RHYTHM.label, "hits": 0,
            "points": round(rhythm_points, 1), "weight": _RHYTHM.weight,
            "advice": _RHYTHM.advice, "shown": 0,
            "note": f"句子长短的起伏只有 {cv:.2f}，正常写作在 {_RHYTHM_OK} 以上，"
                    "整篇读下来像一台机器在匀速输出。",
        })

    total = min(100, int(round(score)))
    dimensions.sort(key=lambda d: -d["points"])
    findings.sort(key=lambda f: f["line"])
    return {
        "score": total,
        "level": _level(total),
        "chars": chars,
        "dimensions": dimensions,
        "findings": findings,
    }
