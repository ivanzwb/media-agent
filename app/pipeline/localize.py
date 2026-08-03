from __future__ import annotations

import hashlib
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
_DIRECT_VIDEO_LINK_RE = re.compile(
    r"\.(?:mp4|webm|ogg|ogv|mov|m4v|m3u8)(?:[?#]|$)", re.I)


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
    direct_links = [
        url for url in _MD_VIDEO_LINK_RE.findall(content)
        if _DIRECT_VIDEO_LINK_RE.search(url)
    ]
    return _dedupe_keep(_videos_from_html(content)
                        + direct_links)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}

# An article's image list is every <img> on the page, so the photographs
# arrive mixed in with the site's furniture: comment/share icons, logos,
# QR codes, avatars, back-to-top arrows. Two cheap checks keep that out of
# a draft — the URL before downloading, the pixels after.
_FURNITURE_WORDS = (
    "qrcode", "qr-code", "qr_code", "ewm", "erweima", "zxcode",
    "logo", "favicon", "icon", "avatar", "sprite", "spacer",
    "placeholder", "watermark", "share", "weixin", "wechat",
    "app-download", "appdownload", "download-app", "button", "btn",
    "backtop", "gotop", "jiucuo",
)

# Real article photos are hundreds of pixels on their short side; the icons
# that keep slipping through measure 66×66, and banner strips 441×62.
_MIN_CONTENT_SIDE = 200

_SVG_ATTR_RE = re.compile(r'\b(width|height)\s*=\s*["\']\s*([\d.]+)\s*(?:px)?'
                          r'\s*["\']', re.I)
_SVG_VIEWBOX_RE = re.compile(
    r'viewBox\s*=\s*["\']\s*[-\d.]+[,\s]+[-\d.]+[,\s]+'
    r'([\d.]+)[,\s]+([\d.]+)', re.I)


def is_page_furniture(url: str) -> bool:
    """Does this URL look like site chrome rather than an article picture?"""
    # The path only: image CDNs carry whole other URLs in the query string.
    path = urlsplit(url).path.lower()
    return any(word in path for word in _FURNITURE_WORDS)


def _svg_size(path: Path) -> tuple[float, float] | None:
    """Read an SVG's drawing size, from width/height or failing that viewBox."""
    try:
        head = path.read_text("utf-8", errors="ignore")[:2000]
    except OSError:
        return None
    attrs = {m.group(1).lower(): m.group(2)
             for m in _SVG_ATTR_RE.finditer(head)}
    try:
        if "width" in attrs and "height" in attrs:
            return float(attrs["width"]), float(attrs["height"])
    except ValueError:  # a percentage or other relative unit
        pass
    box = _SVG_VIEWBOX_RE.search(head)
    return (float(box.group(1)), float(box.group(2))) if box else None


def is_content_image(path: Path) -> bool:
    """Is the file big enough to be a picture worth showing?

    Anything we cannot measure is kept — wrongly dropping a real photo costs
    more than leaving one stray icon in.
    """
    if path.suffix.lower() == ".svg":
        size = _svg_size(path)
        return size is None or min(size) >= _MIN_CONTENT_SIDE
    try:
        from PIL import Image
        with Image.open(path) as im:
            width, height = im.size
    except Exception:  # noqa: BLE001 - unreadable or not a raster image
        return True
    return min(width, height) >= _MIN_CONTENT_SIDE

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
                     download_images: bool = True,
                     download_videos: bool = True,
                     max_images: int = 20,
                     relativize: bool = True) -> Article:
    """Download an article's images and videos into data/media/<hash>/ and
    rewrite article.images / article.videos to local web paths
    (/media/<hash>/<file>), AND replace those URLs inside article.content_md so
    the body references the downloaded files (images AND videos). Downloads run
    concurrently (config.workers). Remote URLs that fail are kept as-is.

    When *relativize* is True the body uses file-relative paths
    (``../../media/...``), suitable for archive markdown opened outside the app.
    When False it keeps absolute ``/media/...`` paths, which the web app serves
    directly (used by the in-app draft editor so localized media renders)."""
    emit = progress or _noop
    folder = article.fingerprint()[:16]
    dest = config.media_dir / folder
    dest.mkdir(parents=True, exist_ok=True)
    workers = config.workers

    imgs = (article.images or [])[:max_images]
    orig_videos = list(article.videos or [])
    emit(f"图片 {len(imgs)} 张，视频 {len(orig_videos)} 个（并发 {workers}）")

    if download_images:
        article.images = _download_set(imgs, dest, folder, _download_image,
                                        workers, emit,
                                        source_url=article.url)
    else:
        emit("  图片本地化已禁用，跳过")

    if download_videos and orig_videos:
        # yt-dlp spawns subprocesses — cap video concurrency lower.
        vworkers = max(1, min(workers, 2))
        article.videos = _download_set(orig_videos, dest, folder,
                                       _download_video, vworkers, emit,
                                       source_url=article.url)
    elif not download_videos and orig_videos:
        emit("  视频本地化已禁用，跳过")

    # Rewrite remote image/video URLs in the body with their local paths so the
    # body's <img>/<iframe>/links (and the front-matter) reference local files.
    if article.content_md:
        mapping: dict[str, str] = {}
        for old, new in zip(imgs, article.images):
            if new != old and _is_local(new):
                mapping[old] = new
        for old, new in zip(orig_videos, article.videos):
            if new != old and _is_local(new):
                mapping[old] = new
        replaced = 0
        for old, new in mapping.items():
            target = _relativize_media(new) if relativize else new
            if old and old in article.content_md:
                article.content_md = article.content_md.replace(old, target)
                replaced += 1
        if replaced:
            emit(f"正文链接已更新 {replaced} 处")

    return article


def localize_reference_images(urls: list[str], config: Config, progress=None,
                              source_url: str | None = None,
                              max_images: int = 12) -> dict[str, str]:
    """Download a set of reference image URLs into data/media/<folder>/ and
    return a map {original_url: /media/<folder>/<name>} for the URLs that were
    localized (already-local or failed URLs are excluded from the map).

    Used to materialize the images a draft's [[IMG:N]] placeholders reference.
    """
    emit = progress or _noop
    urls = [u for u in (urls or []) if u][:max_images]
    if not urls:
        return {}
    # Stable folder per URL set: same references resolve to the same folder.
    folder = hashlib.sha1(
        "|".join(sorted(urls)).encode("utf-8")).hexdigest()[:16]
    dest = config.media_dir / folder
    dest.mkdir(parents=True, exist_ok=True)
    workers = config.workers
    emit(f"下载参考配图 {len(urls)} 张（并发 {workers}）")
    local = _download_set(urls, dest, folder, _download_image, workers,
                          emit, source_url=source_url)
    mapping: dict[str, str] = {}
    furniture = 0
    for old, new in zip(urls, local):
        if new == old or not _is_local(new):
            continue
        # Measured here rather than guessed from the URL: a 66×66 comment
        # icon and a photograph can sit under the same-looking path.
        if not is_content_image(dest / Path(new).name):
            furniture += 1
            continue
        mapping[old] = new
    if furniture:
        emit(f"其中 {furniture} 张是图标/角标类小图，已跳过")
    emit(f"参考配图本地化完成 {len(mapping)}/{len(urls)} 张")
    return mapping


def localize_one(article_id: int, store, config: Config, progress=None,
                 download_images: bool = True,
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
                     download_images=download_images,
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
