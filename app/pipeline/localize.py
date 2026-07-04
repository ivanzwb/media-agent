from __future__ import annotations

import html
import mimetypes
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse, urlsplit, urlunsplit

import httpx

from app.config import Config
from app.models import Article
from app.sources.extractor import _videos_from_html
from app.media.downloader import MediaDownloader
from app.media.strategies import (
    HttpxDirectStrategy,
    UrlTransformStrategy,
    CurlCffiImageStrategy,
    HttpxDirectVideoStrategy,
    YtDlpStrategy,
)

_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
_HTML_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
_MD_VIDEO_LINK_RE = re.compile(r"\[\u25b6[^\]]*\]\(([^)\s]+)\)")


def _dedupe_keep(urls: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for u in urls:
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def content_images(content: str) -> list[str]:
    """Images actually referenced in the article body (markdown + HTML)."""
    return _dedupe_keep(_MD_IMG_RE.findall(content)
                        + _HTML_IMG_RE.findall(content))


def content_videos(content: str) -> list[str]:
    """Videos referenced in the article body (HTML iframes/video + md links)."""
    return _dedupe_keep(_videos_from_html(content)
                        + _MD_VIDEO_LINK_RE.findall(content))

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}

# ---------------------------------------------------------------------------
# Strategy-based media downloaders (fallback chain)
# ---------------------------------------------------------------------------

_image_downloader = MediaDownloader("image")
_image_downloader.add_strategy(HttpxDirectStrategy())
_image_downloader.add_strategy(UrlTransformStrategy())
_image_downloader.add_strategy(CurlCffiImageStrategy())  # bypasses Cloudflare

_video_downloader = MediaDownloader("video")
_video_downloader.add_strategy(HttpxDirectVideoStrategy())
_video_downloader.add_strategy(YtDlpStrategy())

# ---------------------------------------------------------------------------


def _noop(*_a, **_k) -> None:
    pass


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/" if p.scheme and p.netloc else url


def normalize_url(url: str) -> str | None:
    """Make a fetchable absolute http(s) URL: add scheme for //host paths,
    IDNA-encode the host, percent-encode non-ASCII path/query. Returns None
    for relative/unsupported URLs."""
    # Decode HTML entities first (e.g. &amp; -> &) so URLs extracted from HTML
    # (Contentful/WordPress/etc.) aren't sent with literal &amp; -> 400.
    u = html.unescape((url or "").strip())
    if u.startswith("//"):
        u = "https:" + u
    if not u.startswith(("http://", "https://")):
        return None
    parts = urlsplit(u)
    netloc = parts.netloc
    try:
        netloc.encode("ascii")
    except UnicodeEncodeError:
        host = parts.hostname or ""
        try:
            host = host.encode("idna").decode("ascii")
        except Exception:  # noqa: BLE001
            return None
        netloc = host + (f":{parts.port}" if parts.port else "")
    # keep already-encoded %xx intact (safe includes '%')
    path = quote(parts.path, safe="/%:@&=+$,;~()*!'")
    query = quote(parts.query, safe="=&%:/?@+$,;~()*!'")
    return urlunsplit((parts.scheme, netloc, path, query, ""))


def _is_local(url: str) -> bool:
    return url.startswith("/media/")


def _relativize_media(url: str) -> str:
    """Convert absolute /media/ or /images/ paths to relative paths so
    markdown files work both in the web app and in external MD editors.
    /media/abc/pic.png → ../../media/abc/pic.png"""
    if url.startswith("/media/"):
        return f"../../media/{url[len('/media/'):]}"
    if url.startswith("/images/"):
        return f"../../images/{url[len('/images/'):]}"
    return url


def _img_ext(url: str, content_type: str | None) -> str:
    ext = Path(urlparse(url).path).suffix.lower()
    if ext in _IMG_EXTS:
        return ext
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed
    return ".jpg"


def _download_image(url: str, dest: Path, idx: int, emit,
                    source_url: str | None = None) -> Path | None:
    return _image_downloader.download(url, dest, idx, emit,
                                      source_url=source_url)


_DIRECT_VIDEO_EXTS = {".mp4", ".webm", ".m4v", ".mov", ".ogv", ".ogg"}


def _download_video(url: str, dest: Path, idx: int, emit,
                    source_url: str | None = None,
                    max_seconds: int = 60) -> Path | None:
    return _video_downloader.download(url, dest, idx, emit,
                                      source_url=source_url,
                                      max_seconds=max_seconds)


def _download_set(urls, dest: Path, folder: str, fn, workers: int,
                  emit, source_url: str | None = None) -> list[str]:
    """Download `urls` concurrently via `fn(url, dest, idx, emit)`, preserving
    order. Local (/media/) URLs pass through; failures keep the original URL."""
    results: dict[int, Path | None] = {}
    todo = [(i, u) for i, u in enumerate(urls, 1) if not _is_local(u)]
    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(fn, u, dest, i, emit, source_url): i
                     for i, u in todo}
            for f in as_completed(futs):
                i = futs[f]
                try:
                    results[i] = f.result()
                except Exception as e:  # noqa: BLE001
                    emit(f"    下载失败 #{i}：{e}")
                    results[i] = None
    out: list[str] = []
    for i, u in enumerate(urls, 1):
        if _is_local(u):
            out.append(u)
        else:
            p = results.get(i)
            out.append(f"/media/{folder}/{p.name}" if p else u)
    return out


def localize_article(article: Article, config: Config, progress=None,
                     download_videos: bool = True,
                     max_images: int = 20) -> Article:
    """Download an article's images and videos into data/media/<hash>/ and
    rewrite article.images / article.videos to local web paths
    (/media/<hash>/<file>). Downloads run concurrently (config.workers).
    Remote URLs that fail are kept as-is (fallback)."""
    emit = progress or _noop
    folder = article.fingerprint()[:16]
    dest = config.media_dir / folder
    dest.mkdir(parents=True, exist_ok=True)
    workers = config.workers

    imgs = (article.images or [])[:max_images]
    emit(f"图片 {len(imgs)} 张，视频 {len(article.videos or [])} 个"
         f"（并发 {workers}）")
    article.images = _download_set(imgs, dest, folder, _download_image,
                                    workers, emit,
                                    source_url=article.url)

    # Replace remote image URLs in the article body with local paths so
    # the body's <img> tags and the images front-matter don't duplicate.
    mapping: dict[str, str] = {}
    if article.content_md:
        for old, new in zip(imgs, article.images):
            if new != old and _is_local(new):
                mapping[old] = new
        for old, new in mapping.items():
            rel_new = _relativize_media(new) if _is_local(new) else new
            article.content_md = article.content_md.replace(old, rel_new)

    if download_videos and article.videos:
        # yt-dlp spawns subprocesses — cap video concurrency lower.
        vworkers = max(1, min(workers, 2))
        article.videos = _download_set(article.videos, dest, folder,
                                       _download_video, vworkers, emit,
                                       source_url=article.url)

    return article


def localize_one(article_id: int, store, config: Config, progress=None,
                 download_videos: bool = True) -> dict:
    """Localize a single archived article's media (used before transcribing)."""
    emit = progress or _noop
    row = store.get_article(article_id)
    if not row:
        raise ValueError("文章不存在")
    body = store.read_article_body(article_id)
    content = body.get("content_md", "") or ""
    # Images: prefer what the body actually references (skips logos/ads in the
    # whole-page front-matter dump); fall back to front-matter if none found.
    orig_images = content_images(content) or list(body.get("images") or [])
    # Videos: trafilatura strips <video>/<iframe> from the body, so the real
    # video URLs live in front-matter (captured from the original HTML at fetch
    # time); also include any markdown video links present in the body.
    orig_videos = _dedupe_keep(list(body.get("videos") or [])
                               + content_videos(content))
    # Fallback: older archives may not have captured videos. Re-fetch the
    # original page HTML and extract video sources now.
    if not orig_videos and row["url"]:
        emit("未发现视频，尝试重新抓取原页提取…")
        try:
            ref = normalize_url(row["url"]) or row["url"]
            html = httpx.get(ref, timeout=20.0, follow_redirects=True,
                             headers={"User-Agent": _UA}).text
            orig_videos = _videos_from_html(html, base_url=row["url"])
            if not orig_videos:
                # Many SPA/Next.js pages put the mp4 URL in embedded JSON
                # rather than a <video> tag — scan for direct .mp4 links.
                orig_videos = _dedupe_keep(
                    re.findall(r'https?://[^\s"\'<>\\]+\.mp4', html))
            emit(f"  重新提取到视频 {len(orig_videos)} 个")
        except Exception as e:  # noqa: BLE001
            emit(f"  重新抓取失败：{e}")
    emit(f"图片 {len(orig_images)} 张，视频 {len(orig_videos)} 个")
    before = sum(1 for u in orig_images + orig_videos if _is_local(u))

    art = Article(
        title=row["title"], content_md=content, url=row["url"],
        source_name=row["source_name"] or "",
        source_type=row["source_type"] or "scrape",
        published_at=None, images=list(orig_images), raw_summary=None,
        fetched_at=datetime.now(timezone.utc), videos=list(orig_videos))
    emit(f"本地化：{(row['title'] or '')[:40]}")
    localize_article(art, config, progress=lambda m: emit("  " + m),
                     download_videos=download_videos)

    # Replace any newly-localized remote URLs inside the article body too, so
    # the archived markdown references local files (image markdown / iframes).
    mapping: dict[str, str] = {}
    for old, new in zip(orig_images, art.images):
        if new != old and _is_local(new):
            mapping[old] = new
    for old, new in zip(orig_videos, art.videos):
        if new != old and _is_local(new):
            mapping[old] = new
    new_content = content
    for old, new in mapping.items():
        rel_new = _relativize_media(new) if _is_local(new) else new
        new_content = new_content.replace(old, rel_new)

    store.update_article_archive(article_id, new_content, art.images,
                                 art.videos)

    after = sum(1 for u in art.images + art.videos if _is_local(u))
    downloaded = max(0, after - before)
    emit(f"完成：新增本地媒体 {downloaded} 个，正文替换 {len(mapping)} 处")
    return {"downloaded": downloaded, "replaced": len(mapping),
            "images": len(art.images), "videos": len(art.videos)}


def local_media_file(url: str, config: Config) -> Path | None:
    """Resolve a /media/<folder>/<name> web path to a local file (or None)."""
    if not _is_local(url):
        return None
    rel = url[len("/media/"):]
    p = (config.media_dir / rel).resolve()
    base = config.media_dir.resolve()
    if base in p.parents and p.exists():
        return p
    return None
