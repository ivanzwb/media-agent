from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.discovery import search_web

logger = logging.getLogger(__name__)

AVAILABLE_SEARCH_ENGINES = ("bing", "baidu", "duckduckgo", "google", "brave")
DEFAULT_SEARCH_ENGINES = ("bing", "baidu", "duckduckgo", "google", "brave")
# Engines with a dedicated scraper here; the rest are DDGS backends.
_NATIVE_ENGINES = ("bing", "baidu")
SEARCH_RETRIES = 3
_RETRY_BASE_DELAY = 0.4
# How long a faster engine waits for a slower sibling to contribute results
# before returning what already arrived.
_PARALLEL_GRACE_SECONDS = 2.0

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
        # queries need two matching bigrams so a multi-concept query is not
        # satisfied by a landing page matching only its leading word.
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
    ddgs_engines = [engine for engine in selected
                    if engine not in _NATIVE_ENGINES]
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


def _is_baidu_surface_url(url: str) -> bool:
    """Reject Baidu ads and its non-article surfaces (images, video, maps)."""
    parts = urlsplit(url)
    host = parts.netloc.removeprefix("www.")
    if host in ("image.baidu.com", "v.baidu.com", "map.baidu.com"):
        return True
    # ``/s`` and ``/sf`` are Baidu's own result surfaces; some hits resolve
    # back to them instead of to an article.
    return host == "baidu.com" and parts.path.startswith(
        ("/baidu.php", "/s", "/sf"))


def search_baidu_text(query: str, *, max_results: int = 10,
                      proxy: str | None = None,
                      timeout: int = 8) -> list[SearchHit]:
    """Search Baidu HTML, the most reliable backend for Chinese queries.

    Result links are ``baidu.com/link?url=`` redirects, so they are resolved
    to their destinations; otherwise every source would be archived and cited
    as ``baidu.com``. Baidu has no dependable freshness parameter, so results
    are not restricted by the caller's time range.
    """
    from lxml import html as lxml_html

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"),
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    with httpx.Client(proxy=proxy, timeout=timeout, follow_redirects=True,
                      headers=headers) as client:
        response = client.get(
            "https://www.baidu.com/s",
            params={"wd": query, "rn": str(max_results)})
        response.raise_for_status()

        document = lxml_html.fromstring(
            response.content.decode("utf-8", errors="replace"))
        parsed: list[SearchHit] = []
        seen_links: set[str] = set()
        for heading in document.xpath("//h3"):
            links = heading.xpath(".//a[@href]")
            if not links:
                continue
            link = links[0]
            url = normalize_search_url(str(link.get("href") or ""))
            if not url or url in seen_links or _is_baidu_surface_url(url):
                continue
            seen_links.add(url)
            container = heading.getparent()
            snippet = ""
            if container is not None:
                snippet = " ".join(" ".join(container.xpath(
                    './/*[contains(@class, "content-right_") or '
                    'contains(@class, "c-abstract")]//text()')).split())
            parsed.append(SearchHit(
                title=" ".join(link.text_content().split()) or url,
                url=url,
                snippet=snippet))
            if len(parsed) >= max_results:
                break

        def resolve(hit: SearchHit) -> SearchHit:
            try:
                final = normalize_search_url(str(client.head(hit.url).url))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Baidu 跳转解析失败 %s：%s", hit.url, exc)
                return hit
            if not final or final == hit.url:
                return hit
            return SearchHit(hit.title, final, hit.snippet)

        if not parsed:
            return []
        with ThreadPoolExecutor(max_workers=min(6, len(parsed))) as executor:
            resolved = list(executor.map(resolve, parsed))

    hits: list[SearchHit] = []
    seen: set[str] = set()
    for hit in resolved:
        if hit.url in seen or _is_baidu_surface_url(hit.url):
            continue
        seen.add(hit.url)
        hits.append(hit)
    return hits


def _run_engine_once(
        query: str, engine: str, *, max_results: int,
        timelimit: str | None, region: str | None,
        proxy: str | None, timeout: int,
) -> list[SearchHit]:
    if engine == "bing":
        return search_bing_text(
            query, max_results=max_results, timelimit=timelimit,
            region=region, proxy=proxy, timeout=timeout)
    if engine == "baidu":
        return search_baidu_text(
            query, max_results=max_results, proxy=proxy, timeout=timeout)
    return search_ddgs_text(
        query, max_results=max_results, timelimit=timelimit,
        region=region, engines=[engine], proxy=proxy, timeout=timeout)


def _search_engine_with_retries(
        query: str, engine: str, *, max_results: int,
        timelimit: str | None, region: str | None,
        proxy: str | None, timeout: int,
        on_detail: Callable[[str], None] | None = None,
) -> list[SearchHit]:
    """Search one engine with retries; return only hits relevant to the query."""
    for attempt in range(SEARCH_RETRIES + 1):
        try:
            hits = _run_engine_once(
                query, engine, max_results=max_results, timelimit=timelimit,
                region=region, proxy=proxy, timeout=timeout)
            if on_detail:
                on_detail(f"{engine} 返回 {len(hits)} 条")
            relevant_hits = filter_hits_for_query(hits, query)
            if relevant_hits:
                if on_detail:
                    on_detail(f"{engine} 保留 {len(relevant_hits)} 条相关结果")
                return relevant_hits
            if hits:
                logger.warning(
                    "%s 搜索 %r 返回 %d 条，但与完整检索词相关性不足",
                    engine, query, len(hits))
                if on_detail:
                    on_detail(f"{engine} 结果与检索词相关性不足")
                break
            error_detail = "未返回结果"
        except Exception as exc:  # noqa: BLE001
            error_detail = str(exc)
            if on_detail:
                on_detail(f"{engine} 失败：{error_detail}")
        if attempt >= SEARCH_RETRIES:
            logger.warning(
                "%s 搜索 %r 连续重试 %d 次后仍失败：%s",
                engine, query, SEARCH_RETRIES, error_detail)
            break
        retry_number = attempt + 1
        delay = _RETRY_BASE_DELAY * (2 ** attempt)
        logger.warning(
            "%s 搜索 %r 失败：%s；%.1f 秒后进行第 %d/%d 次重试",
            engine, query, error_detail, delay, retry_number, SEARCH_RETRIES)
        time.sleep(delay)
    return []


def _search_in_parallel(
        query: str, engines: list[str], *, max_results: int,
        timelimit: str | None, region: str | None,
        proxy: str | None, timeout: int,
        on_detail: Callable[[str], None] | None = None,
) -> list[SearchHit]:
    """Run independent engines concurrently and merge their relevant results.

    Returns as soon as relevant hits arrive, giving still-running siblings a
    short grace window to contribute their own results first. Engines that do
    not finish within the grace period are abandoned — their thread finishes
    in the background, so one down engine's retries never block a healthy
    engine's results. A non-empty-but-irrelevant response (which the per-engine
    retry loop treats as terminal) also falls through here.
    """
    if not engines:
        return []
    groups: list[list[SearchHit]] = []
    executor = ThreadPoolExecutor(max_workers=len(engines))
    futures = {
        executor.submit(
            _search_engine_with_retries, query, engine,
            max_results=max_results, timelimit=timelimit, region=region,
            proxy=proxy, timeout=timeout, on_detail=on_detail,
        ): engine
        for engine in engines
    }
    try:
        for future in as_completed(futures):
            try:
                hits = future.result()
            except Exception:  # noqa: BLE001
                hits = []
            if not hits:
                continue
            groups.append(hits)
            pending = [item for item in futures if not item.done()]
            if not pending:
                break
            done, _ = wait(pending, timeout=_PARALLEL_GRACE_SECONDS)
            for sibling in done:
                try:
                    sibling_hits = sibling.result()
                except Exception:  # noqa: BLE001
                    sibling_hits = []
                if sibling_hits:
                    groups.append(sibling_hits)
            break
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return merge_search_hits(groups)


def search_query(query: str, *, max_results: int = 10,
                 timelimit: str | None = None,
                 region: str | None = None,
                 engines: tuple[str, ...] | list[str] | None = None,
                 proxy: str | None = None,
                 timeout: int = 8,
                 on_detail: Callable[[str], None] | None = None,
                 ) -> list[SearchHit]:
    selected = tuple(engines or DEFAULT_SEARCH_ENGINES)
    native = [engine for engine in _NATIVE_ENGINES if engine in selected]
    backends = [engine for engine in selected if engine not in _NATIVE_ENGINES]

    # Wave 1: run the selected native engines (Bing, Baidu) concurrently and
    # merge their hits. Relying on a single engine serial-first meant a
    # transient Bing outage silently reduced the search to one sparse Baidu
    # fallback; running both in parallel keeps the richer Chinese results even
    # when Bing is down, without paying for the loser's retries.
    hits = _search_in_parallel(
        query, native, max_results=max_results, timelimit=timelimit,
        region=region, proxy=proxy, timeout=timeout, on_detail=on_detail)
    if hits:
        return hits

    # Wave 2: DDGS backends, tried in parallel with the same grace logic.
    hits = _search_in_parallel(
        query, backends, max_results=max_results, timelimit=timelimit,
        region=region, proxy=proxy, timeout=timeout, on_detail=on_detail)
    if hits:
        return hits

    # Bing is a pragmatic final fallback in networks where Google and
    # DuckDuckGo are blocked, even for clients using an older engine list.
    if "bing" not in selected:
        hits = _search_in_parallel(
            query, ["bing"], max_results=max_results, timelimit=timelimit,
            region=region, proxy=proxy, timeout=timeout, on_detail=on_detail)
        if hits:
            return hits

    # Bing's automated Chinese endpoint sometimes searches only the first
    # query fragment, while DDGS backends can be unavailable on mainland
    # networks. Baidu is therefore a useful final fallback for Chinese topics.
    if region == "cn-zh" and "baidu" not in selected:
        try:
            baidu_hits = search_baidu_text(
                query, max_results=max_results, proxy=proxy, timeout=timeout)
            relevant_hits = filter_hits_for_query(baidu_hits, query)
            if relevant_hits:
                logger.info(
                    "Baidu 中文降级搜索 %r 返回 %d 条相关结果",
                    query, len(relevant_hits))
                if on_detail:
                    on_detail(f"baidu 中文降级保留 {len(relevant_hits)} 条相关结果")
                return relevant_hits
            if baidu_hits:
                logger.warning(
                    "Baidu 中文降级搜索 %r 返回 %d 条，但相关性不足",
                    query, len(baidu_hits))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Baidu 中文降级搜索 %r 失败：%s", query, exc)
            if on_detail:
                on_detail(f"baidu 中文降级失败：{exc}")

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
