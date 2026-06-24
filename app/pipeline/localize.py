from __future__ import annotations

import mimetypes
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse, urlsplit, urlunsplit

import httpx

from app.config import Config
from app.models import Article
from app.sources.extractor import _videos_from_html

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


def _noop(*_a, **_k) -> None:
    pass


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/" if p.scheme and p.netloc else url


def normalize_url(url: str) -> str | None:
    """Make a fetchable absolute http(s) URL: add scheme for //host paths,
    IDNA-encode the host, percent-encode non-ASCII path/query. Returns None
    for relative/unsupported URLs."""
    u = (url or "").strip()
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


def _img_ext(url: str, content_type: str | None) -> str:
    ext = Path(urlparse(url).path).suffix.lower()
    if ext in _IMG_EXTS:
        return ext
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed
    return ".jpg"


def _download_image(url: str, dest: Path, idx: int, emit) -> Path | None:
    safe = normalize_url(url)
    if not safe:
        emit(f"    跳过无效图片地址 #{idx}：{(url or '')[:60]}")
        return None
    try:
        resp = httpx.get(safe, timeout=20.0, follow_redirects=True,
                         headers={"User-Agent": _UA, "Referer": _origin(safe)})
        resp.raise_for_status()
        data = resp.content
    except Exception as e:  # noqa: BLE001
        emit(f"    图片下载失败 #{idx}：{e}")
        return None
    if not data:
        return None
    ext = _img_ext(url, resp.headers.get("content-type"))
    path = dest / f"img-{idx}{ext}"
    path.write_bytes(data)
    return path


_DIRECT_VIDEO_EXTS = {".mp4", ".webm", ".m4v", ".mov", ".ogv", ".ogg"}


def _download_video(url: str, dest: Path, idx: int, emit,
                    max_seconds: int = 60) -> Path | None:
    safe = normalize_url(url)
    if not safe:
        emit(f"    跳过无效视频地址 #{idx}：{(url or '')[:80]}")
        return None
    ext = Path(urlparse(safe).path).suffix.lower()

    # Direct media files (e.g. figure.ai self-hosted mp4) → download directly.
    if ext in _DIRECT_VIDEO_EXTS:
        emit(f"    直链下载视频 #{idx}（{ext}）：{safe[:80]}")
        path = dest / f"vid-{idx}{ext}"
        try:
            with httpx.stream("GET", safe, timeout=120.0,
                              follow_redirects=True,
                              headers={"User-Agent": _UA,
                                       "Referer": _origin(safe)}) as r:
                r.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in r.iter_bytes(chunk_size=65536):
                        f.write(chunk)
        except Exception as e:  # noqa: BLE001
            emit(f"    直链视频下载失败 #{idx}：{e}")
            return None
        emit(f"    视频已保存 #{idx}：{path.name}")
        return path

    # Otherwise treat as a platform embed (YouTube/Bilibili/…) → yt-dlp.
    if not shutil.which("yt-dlp"):
        emit(f"    无 yt-dlp，无法下载平台视频 #{idx}：{safe[:80]}")
        return None
    emit(f"    yt-dlp 下载视频 #{idx}：{safe[:80]}")
    template = str(dest / f"vid-{idx}.%(ext)s")
    cmd = ["yt-dlp", "--no-playlist", "--no-warnings",
           "--download-sections", f"*0-{max_seconds}",
           "-f", "best[ext=mp4]/best", "--merge-output-format", "mp4",
           "-o", template, safe]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (subprocess.SubprocessError, OSError) as e:
        emit(f"    视频下载失败 #{idx}：{e}")
        return None
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-1:] or [""]
        emit(f"    视频下载失败 #{idx}：{tail[0][:160]}")
        return None
    matches = sorted(dest.glob(f"vid-{idx}.*"))
    if matches:
        emit(f"    视频已保存 #{idx}：{matches[0].name}")
        return matches[0]
    emit(f"    视频下载后未找到文件 #{idx}")
    return None


def localize_article(article: Article, config: Config, progress=None,
                     download_videos: bool = True,
                     max_images: int = 20) -> Article:
    """Download an article's images and videos into data/media/<hash>/ and
    rewrite article.images / article.videos to local web paths
    (/media/<hash>/<file>). Remote URLs that fail are kept as-is (fallback)."""
    emit = progress or _noop
    folder = article.fingerprint()[:16]
    dest = config.media_dir / folder
    dest.mkdir(parents=True, exist_ok=True)

    imgs = (article.images or [])[:max_images]
    emit(f"图片 {len(imgs)} 张，视频 {len(article.videos or [])} 个")
    local_images: list[str] = []
    for i, url in enumerate(imgs, 1):
        if _is_local(url):
            local_images.append(url)
            continue
        emit(f"  下载图片 #{i}/{len(imgs)}…")
        p = _download_image(url, dest, i, emit)
        local_images.append(f"/media/{folder}/{p.name}" if p else url)
    article.images = local_images

    if download_videos and article.videos:
        local_videos: list[str] = []
        vids = article.videos
        for i, url in enumerate(vids, 1):
            if _is_local(url):
                local_videos.append(url)
                continue
            emit(f"  下载视频 #{i}/{len(vids)}…")
            p = _download_video(url, dest, i, emit)
            local_videos.append(f"/media/{folder}/{p.name}" if p else url)
        article.videos = local_videos

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
        new_content = new_content.replace(old, new)

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
