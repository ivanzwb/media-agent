from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.discovery import search_web

logger = logging.getLogger(__name__)

AVAILABLE_SEARCH_ENGINES = ("bing", "duckduckgo", "google", "brave")
DEFAULT_SEARCH_ENGINES = ("bing", "duckduckgo", "google", "brave")
SEARCH_RETRIES = 3
_RETRY_BASE_DELAY = 0.4

_TRACKING = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "ref_src", "spm",
}


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str = ""


def filter_hits_for_query(hits: list[SearchHit], query: str) -> list[SearchHit]:
    """Discard broad search results that only match one generic query fragment."""
    lowered = (query or "").lower()
    latin_terms = set(re.findall(r"[a-z0-9]{2,}", lowered))
    cjk_groups = re.findall(r"[\u3400-\u9fff]{2,}", lowered)
    cjk_terms = {
        group[index:index + 2]
        for group in cjk_groups
        for index in range(len(group) - 1)
    }
    if not latin_terms and not cjk_terms:
        return hits

    filtered: list[SearchHit] = []
    for hit in hits:
        haystack = f"{hit.title} {hit.snippet}".lower()
        latin_matches = sum(term in haystack for term in latin_terms)
        cjk_matches = sum(term in haystack for term in cjk_terms)
        # One Latin term is usually discriminative (AI, OpenAI, GPU). Chinese
        # natural-language questions need two matching bigrams so Bing cannot
        # satisfy “儿童美甲健康风险” with a generic “儿童” landing page.
        if latin_matches >= 1 or cjk_matches >= min(2, len(cjk_terms)):
            filtered.append(hit)
    return filtered


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
                     region: str | None = None,
                     engines: tuple[str, ...] | list[str] | None = None,
                     proxy: str | None = None,
                     timeout: int = 8,
                     ) -> list[SearchHit]:
    from ddgs import DDGS

    selected = tuple(engines or DEFAULT_SEARCH_ENGINES)
    ddgs_engines = [engine for engine in selected if engine != "bing"]
    if not ddgs_engines:
        return []
    rows = DDGS(proxy=proxy, timeout=timeout).text(
        query,
        region=region or "wt-wt",
        timelimit=timelimit,
        max_results=max_results,
        backend=",".join(ddgs_engines),
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


def search_bing_text(query: str, *, max_results: int = 10,
                     timelimit: str | None = None,
                     region: str | None = None,
                     proxy: str | None = None,
                     timeout: int = 8) -> list[SearchHit]:
    """Search Bing's HTML results, useful where Google/DDG are unreachable."""
    from lxml import html as lxml_html

    freshness = {"d": "Day", "w": "Week", "m": "Month"}.get(timelimit or "")
    params = {
        "q": query,
        "count": str(max_results),
        "setlang": "zh-hans" if region == "cn-zh" else "en",
    }
    if freshness:
        params["filters"] = f'ex1:"ez{freshness}"'
    with httpx.Client(
            proxy=proxy, timeout=timeout, follow_redirects=True,
            headers={"User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/126.0 Safari/537.36")}) as client:
        response = client.get("https://cn.bing.com/search", params=params)
        response.raise_for_status()
    # cn.bing.com occasionally omits/incorrectly reports the charset, causing
    # httpx to decode Chinese titles as mojibake.
    response.encoding = "utf-8"
    document = lxml_html.fromstring(response.text)
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for item in document.xpath(
            '//li[contains(concat(" ", normalize-space(@class), " "),'
            ' " b_algo ")]'):
        links = item.xpath(".//h2/a[@href]")
        if not links:
            continue
        link = links[0]
        url = normalize_search_url(str(link.get("href") or ""))
        if not url or url in seen:
            continue
        seen.add(url)
        title = " ".join(link.text_content().split()) or url
        snippet = " ".join(" ".join(item.xpath(".//p//text()")).split())
        hits.append(SearchHit(title=title, url=url, snippet=snippet))
        if len(hits) >= max_results:
            break
    return hits


def search_query(query: str, *, max_results: int = 10,
                 timelimit: str | None = None,
                 region: str | None = None,
                 engines: tuple[str, ...] | list[str] | None = None,
                 proxy: str | None = None,
                 timeout: int = 8,
                 ) -> list[SearchHit]:
    selected = tuple(engines or DEFAULT_SEARCH_ENGINES)
    if region == "cn-zh":
        selected = ("bing",) + tuple(
            engine for engine in selected if engine != "bing")
    for engine in selected:
        for attempt in range(SEARCH_RETRIES + 1):
            try:
                if engine == "bing":
                    hits = search_bing_text(
                        query, max_results=max_results, timelimit=timelimit,
                        region=region, proxy=proxy, timeout=timeout)
                else:
                    hits = search_ddgs_text(
                        query, max_results=max_results, timelimit=timelimit,
                        region=region, engines=[engine], proxy=proxy,
                        timeout=timeout)
                relevant_hits = filter_hits_for_query(hits, query)
                if relevant_hits:
                    return relevant_hits
                if hits:
                    logger.warning(
                        "%s 搜索 %r 返回 %d 条，但与完整检索词相关性不足",
                        engine, query, len(hits))
                    break
                error_detail = "未返回结果"
            except Exception as exc:  # noqa: BLE001
                error_detail = str(exc)
            if attempt >= SEARCH_RETRIES:
                logger.warning(
                    "%s 搜索 %r 连续重试 %d 次后仍失败：%s",
                    engine, query, SEARCH_RETRIES, error_detail)
                break
            retry_number = attempt + 1
            delay = _RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning(
                "%s 搜索 %r 失败：%s；%.1f 秒后进行第 %d/%d 次重试",
                engine, query, error_detail, delay, retry_number,
                SEARCH_RETRIES)
            time.sleep(delay)

    # Bing is a pragmatic final fallback in networks where Google and
    # DuckDuckGo are blocked, even for clients using an older engine list.
    if "bing" not in selected:
        for attempt in range(SEARCH_RETRIES + 1):
            try:
                hits = search_bing_text(
                    query, max_results=max_results, timelimit=timelimit,
                    region=region, proxy=proxy, timeout=timeout)
                relevant_hits = filter_hits_for_query(hits, query)
                if relevant_hits:
                    return relevant_hits
                if hits:
                    logger.warning(
                        "Bing 降级搜索 %r 返回 %d 条，但相关性不足",
                        query, len(hits))
                    break
                error_detail = "未返回结果"
            except Exception as exc:  # noqa: BLE001
                error_detail = str(exc)
            if attempt >= SEARCH_RETRIES:
                logger.warning(
                    "Bing 降级搜索 %r 连续重试 %d 次后仍失败：%s",
                    query, SEARCH_RETRIES, error_detail)
                break
            time.sleep(_RETRY_BASE_DELAY * (2 ** attempt))

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
