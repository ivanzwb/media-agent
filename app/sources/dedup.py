from __future__ import annotations

import re

from app.models import Article


def dedup(articles: list[Article]) -> list[Article]:
    seen: set[str] = set()
    result: list[Article] = []
    for a in articles:
        fp = a.fingerprint()
        if fp in seen:
            continue
        seen.add(fp)
        result.append(a)
    return result


def _content_signature(article: Article, width: int = 5) -> set[str]:
    text = f"{article.title}\n{article.content_md or article.raw_summary or ''}"
    # Remove markdown URLs and punctuation while retaining CJK characters.
    text = re.sub(r"\]\([^)]+\)", "]", text.lower())
    text = re.sub(r"[^\w\u3400-\u9fff]+", "", text)
    if len(text) < width:
        return {text} if text else set()
    return {text[index:index + width]
            for index in range(len(text) - width + 1)}


def _content_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    overlap = len(left & right)
    return max(
        overlap / len(left | right),
        overlap / min(len(left), len(right)),
    )


def dedup_near_content(articles: list[Article], *,
                       threshold: float = 0.82) -> list[Article]:
    """Remove syndicated or lightly edited copies by text-shingle overlap.

    Search runs are small (normally under 40 pages), so an O(n²) comparison is
    simpler and deterministic. When two pages are near-duplicates, the richer
    body is retained while preserving the first group's position.
    """
    kept: list[tuple[Article, set[str]]] = []
    for article in articles:
        signature = _content_signature(article)
        duplicate_at = None
        for index, (_, existing) in enumerate(kept):
            if _content_similarity(signature, existing) >= threshold:
                duplicate_at = index
                break
        if duplicate_at is None:
            kept.append((article, signature))
            continue
        current, _ = kept[duplicate_at]
        if len(article.content_md or "") > len(current.content_md or ""):
            kept[duplicate_at] = (article, signature)
    return [article for article, _ in kept]
