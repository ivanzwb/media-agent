"""发布前导流体检。

公众号把微信号、二维码和站外链接当成引流，出现在正文、摘要或推广文案里，
文章就拿不到推荐流量。敏感词表只能匹配配置过的词（加微信、扫码…），
`vx: abc123` 这类写法、二维码图片和外链都漏在外面，所以单独做一次体检。

体检只报告，不改稿：命中什么、在第几行，由人自己决定要不要留。
"""

from __future__ import annotations

import re

KIND_LABELS = {
    "wechat_id": "微信号",
    "qr_image": "二维码图片",
    "external_link": "站外链接",
}

# 微信号：6-20 位，字母开头，只含字母数字下划线减号。
_ID = r"[A-Za-z][-_A-Za-z0-9]{5,19}"
# 「vx」「加微」这类写法本身就是联系方式，后面跟什么都算。
_ID_AFTER_MARKETING = re.compile(
    rf"(?:微信号|威信|薇信|wx号|vx|v信|加微信?|加v)\s*[:：=＝、,，]?\s*({_ID})",
    re.I)
# 「微信」「wechat」也可能只是在讲这个产品，要有冒号一类的分隔符才算联系方式。
_ID_AFTER_PLAIN = re.compile(
    rf"(?:微信|weixin|wechat|wx)\s*[:：=＝]\s*({_ID})", re.I)

_QR_HINT = re.compile(r"(?<![a-z])qr|erweima|二维码|扫码|扫一扫|长按识别|长按关注",
                      re.I)

_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(\s*([^)\s]+)")
_HTML_IMAGE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I)
_MD_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(\s*(https?://[^)\s]+)")
_HTML_LINK = re.compile(r"<a\b[^>]*?\bhref\s*=\s*[\"'](https?://[^\"']+)[\"']",
                        re.I)
_BARE_URL = re.compile(r'https?://[^\s<>()\[\]"\'，。；、）】]+')
_FENCE = re.compile(r"^\s*(?:```|~~~)")

# 公众号内部的链接不算站外，平台自己也这么认。
_IN_PLATFORM = ("mp.weixin.qq.com", "weixin.qq.com", "channels.weixin.qq.com")


def _finding(kind: str, text: str, line: int, where: str) -> dict:
    return {"kind": kind, "label": KIND_LABELS[kind],
            "text": text.strip()[:120], "line": line, "where": where}


def _is_in_platform(url: str) -> bool:
    host = url.split("//", 1)[-1].split("/", 1)[0].lower()
    return any(host == d or host.endswith("." + d) for d in _IN_PLATFORM)


def scan_text(text: str, where: str = "正文") -> list[dict]:
    """Report the WeChat IDs, QR pictures and outbound links in *text*.

    Works line by line so a hit can be pointed at, and so a picture URL is
    never mistaken for the outbound link the platform punishes.
    """
    findings: list[dict] = []
    lines = (text or "").splitlines()
    in_fence = False
    for index, line in enumerate(lines):
        line_no = index + 1
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        images: list[tuple[str, str]] = [
            (alt, url) for alt, url in _MD_IMAGE.findall(line)]
        images += [("", url) for url in _HTML_IMAGE.findall(line)]
        # 「长按识别」写在图片上一行或下一行是公众号的常见排法，图基本就是
        # 二维码，哪怕文件名看不出来。
        context = " ".join(lines[max(0, index - 1):index + 2])
        near_qr_words = bool(_QR_HINT.search(context))
        for alt, url in images:
            if near_qr_words or _QR_HINT.search(f"{alt} {url}"):
                findings.append(_finding("qr_image", url, line_no, where))

        # 图片链接不是外链，发布前会被上传成微信素材。
        rest = _HTML_IMAGE.sub(" ", _MD_IMAGE.sub(" ", line))
        links: list[tuple[int, str]] = []
        seen: set[str] = set()
        for pattern in (_MD_LINK, _HTML_LINK, _BARE_URL):
            for match in pattern.finditer(rest):
                url = (match.group(1) if pattern is not _BARE_URL
                       else match.group(0)).rstrip(".,;:!?)")
                if url in seen or _is_in_platform(url):
                    continue
                seen.add(url)
                links.append((match.start(), url))
        findings += [_finding("external_link", url, line_no, where)
                     for _, url in sorted(links)]

        # 一行报一次就够，同一句话里写两遍不必拆成两条。
        match = _ID_AFTER_MARKETING.search(line) or _ID_AFTER_PLAIN.search(line)
        if match:
            findings.append(
                _finding("wechat_id", match.group(0), line_no, where))
    return findings


def check_draft(meta: dict, promotion_footer: str = "") -> list[dict]:
    """Run the check over everything that ships with the article.

    The digest counts: it is the 简介 readers see on the card, and a WeChat ID
    there costs the same recommendation traffic as one in the body.
    """
    meta = meta or {}
    titles = meta.get("title_candidates") or []
    title = meta.get("title_cn") or (titles[0] if titles else "")
    findings = (
        scan_text(str(title or ""), "标题")
        + scan_text(str(meta.get("digest") or ""), "摘要")
        + scan_text(str(meta.get("body_md") or ""), "正文")
    )
    # 推广文案是用户自填的自由文本，可能还没进正文，也可能被模型改写过，
    # 单独再查一遍；已经在正文里报过的就不重复。
    reported = {f["text"] for f in findings}
    findings += [f for f in scan_text(str(promotion_footer or ""), "推广文案")
                 if f["text"] not in reported]
    return findings
