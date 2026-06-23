from __future__ import annotations

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
