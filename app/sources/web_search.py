from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.discovery import search_web

logger = logging.getLogger(__name__)

_TRACKING = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "ref_src", "spm",
}


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str = ""


def normalize_search_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    query = urlencode([
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING
    ])
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def search_ddgs_text(query: str, *, max_results: int = 10,
                     timelimit: str | None = None,
                     region: str | None = None) -> list[SearchHit]:
    from ddgs import DDGS

    rows = DDGS().text(
        query,
        region=region or "wt-wt",
        timelimit=timelimit,
        max_results=max_results,
    )
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for row in rows or []:
        url = normalize_search_url(str(row.get("href") or row.get("url") or ""))
        if not url or url in seen:
            continue
        seen.add(url)
        hits.append(SearchHit(
            title=str(row.get("title") or url),
            url=url,
            snippet=str(row.get("body") or row.get("snippet") or ""),
        ))
        if len(hits) >= max_results:
            break
    return hits


def search_query(query: str, *, max_results: int = 10,
                 timelimit: str | None = None,
                 region: str | None = None) -> list[SearchHit]:
    try:
        hits = search_ddgs_text(
            query, max_results=max_results, timelimit=timelimit, region=region)
        if hits:
            return hits
    except Exception as exc:  # noqa: BLE001
        logger.warning("ddgs text search failed for %r: %s", query, exc)

    return [
        SearchHit(title=url, url=normalized)
        for url in search_web(query, max_results=max_results)
        if (normalized := normalize_search_url(url))
    ]


def merge_search_hits(groups: list[list[SearchHit]]) -> list[SearchHit]:
    merged: list[SearchHit] = []
    seen: set[str] = set()
    for group in groups:
        for hit in group:
            url = normalize_search_url(hit.url)
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append(SearchHit(hit.title, url, hit.snippet))
    return merged
