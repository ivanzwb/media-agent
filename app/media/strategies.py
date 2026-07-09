"""Concrete download strategies for images and videos."""

from __future__ import annotations

import html
import importlib.util
import mimetypes
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse, unquote, parse_qs, urlsplit, urlunsplit, quote as _urlquote

import httpx

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Rotated User-Agents for fallback strategy
_FALLBACK_UAS = [
    _UA,
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 Edg/124.0",
]

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
_DIRECT_VIDEO_EXTS = {".mp4", ".webm", ".m4v", ".mov", ".ogv", ".ogg"}


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/" if p.scheme and p.netloc else url


def _ytdlp_base() -> list[str] | None:
    """Command prefix to run yt-dlp: prefer the CLI, fall back to the installed
    Python module (`python -m yt_dlp`) so it works when only `pip install
    yt-dlp` was done without the CLI on PATH."""
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]
    return None


def _strip_trailing_backslash(url: str) -> str:
    """Remove trailing backslash that gets appended by HTML attribute regex."""
    return url.rstrip("\\")


def _decode_json_escapes(url: str) -> str:
    """Decode \\u00xx unicode escapes (e.g. \\u0026 -> &) that appear in URLs
    extracted from JSON-embedded HTML (Next.js __NEXT_DATA__, RSC streams, etc.)."""
    return re.sub(r'\\u00([0-9a-fA-F]{2})', lambda m: chr(int(m.group(1), 16)), url)


def _first_from_srcset(url: str) -> str:
    """If the URL is a raw srcset attribute value (comma-separated with size
    descriptors like ' 100w' or ' 2x'), return only the first real URL."""
    # If there's a comma followed by http/https, this is a srcset
    if re.search(r',\s*https?://', url):
        first = url.split(",")[0].strip()
        # Strip trailing size descriptor: " 100w", " 2x", " 100w,"
        first = re.sub(r'\s+\d+[whx]\s*,?\s*$', '', first)
        return first
    # Also handle single-URL srcset with size descriptor (no comma)
    # e.g. "https://...jpg?mw=100 100w"
    first = re.sub(r'\s+\d+[whx]\s*$', '', url)
    return first


def normalize_url(url: str) -> str | None:
    """Make a fetchable absolute http(s) URL: add scheme for //host paths,
    IDNA-encode the host, percent-encode non-ASCII path/query. Returns None
    for relative/unsupported URLs."""
    # Decode HTML entities first (e.g. &amp; -> &) so URLs extracted from HTML
    # aren't sent with literal &amp; -> 400.
    u = html.unescape((url or "").strip())

    # --- Pre-processing fixes for common extraction artifacts ---
    u = _strip_trailing_backslash(u)
    u = _decode_json_escapes(u)

    if u.startswith("//"):
        u = "https:" + u
    if not u.startswith(("http://", "https://")):
        return None

    # Handle srcset: if URL contains comma-separated variants, take the first
    if "," in u:
        u = _first_from_srcset(u)

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
    path = _urlquote(parts.path, safe="/%:@&=+$,;~()*!'")
    query = _urlquote(parts.query, safe="=&%:/?@+$,;~()*!'")
    return urlunsplit((parts.scheme, netloc, path, query, ""))


def _img_ext(url: str, content_type: str | None) -> str:
    ext = Path(urlparse(url).path).suffix.lower()
    if ext in _IMG_EXTS:
        return ext
    if content_type:
        mime = content_type.split(";")[0].strip()
        # Python's mimetypes doesn't know about .webp → map manually
        if "webp" in mime:
            return ".webp"
        guessed = mimetypes.guess_extension(mime)
        if guessed and guessed in _IMG_EXTS:
            return ".jpg" if guessed == ".jpe" else guessed
    return ".jpg"


# ---------------------------------------------------------------------------
# Image: direct HTTP download
# ---------------------------------------------------------------------------

class HttpxDirectStrategy:
    """Download image URLs directly via httpx.

    Matches URLs whose path ends with a known image extension, or that contain
    one anywhere (lower confidence).
    """

    name = "httpx_direct"

    def match(self, url: str) -> float:
        ext = Path(urlparse(url).path).suffix.lower()
        if ext in _IMG_EXTS:
            return 0.8
        # URL may contain image extension in query (Next.js etc.)
        if any(e in url.lower() for e in (".jpg", ".jpeg", ".png", ".webp")):
            return 0.3
        return 0.0

    def download(
        self, url: str, dest: Path, idx: int,
        emit: Callable = print, source_url: str | None = None, **kwargs,
    ) -> Path | None:
        safe = normalize_url(url)
        if not safe:
            emit(f"    [httpx_direct] 跳过无效地址 #{idx}：{(url or '')[:60]}")
            return None

        # Use article page as Referer (more likely to be accepted by CDNs)
        # than the image origin.
        referer = source_url or _origin(safe)

        try:
            resp = httpx.get(
                safe, timeout=20.0, follow_redirects=True,
                headers={"User-Agent": _UA, "Referer": referer},
            )
            resp.raise_for_status()
            data = resp.content
        except Exception as e:
            emit(f"    [httpx_direct] 图片下载失败 #{idx}：{e}")
            return None

        if not data:
            return None

        ext = _img_ext(url, resp.headers.get("content-type"))
        path = dest / f"img-{idx}{ext}"
        path.write_bytes(data)
        emit(f"    [httpx_direct] 已保存 #{idx}：{path.name}")
        return path


# ---------------------------------------------------------------------------
# Image: URL transform (decode proxy URLs to real source)
# ---------------------------------------------------------------------------

class UrlTransformStrategy:
    """Decode proxy/transform image URLs to fetch the source image directly.

    Handles:
      - Next.js ``/_next/image?url=...`` — decode the ``url`` query param
      - (extensible with more patterns)
    """

    name = "url_transform"

    _PROXY_PATTERNS: list[tuple[re.Pattern, str]] = [
        (re.compile(r"/_next/image\?"), "url"),
    ]

    def match(self, url: str) -> float:
        for pat, _param in self._PROXY_PATTERNS:
            if pat.search(url):
                return 0.9
        return 0.0

    def download(
        self, url: str, dest: Path, idx: int,
        emit: Callable = print, **kwargs,
    ) -> Path | None:
        for pat, param in self._PROXY_PATTERNS:
            if not pat.search(url):
                continue

            parsed = urlparse(url)
            qs = parse_qs(parsed.query, keep_blank_values=True)
            encoded = qs.get(param, [None])[0]
            if not encoded:
                emit(f"    [url_transform] 未找到参数 `{param}`")
                return None

            real_url = unquote(encoded)
            emit(f"    [url_transform] 解码 → {real_url[:80]}")

            # Decode HTML entities (e.g. &amp; → &) that may survive in query
            real_url = html.unescape(real_url)

            safe = normalize_url(real_url)
            if not safe:
                emit(f"    [url_transform] 解码后地址无效")
                return None

            try:
                resp = httpx.get(
                    safe, timeout=20.0, follow_redirects=True,
                    headers={"User-Agent": _UA, "Referer": _origin(safe)},
                )
                resp.raise_for_status()
                data = resp.content
            except Exception as e:
                emit(f"    [url_transform] 下载失败：{e}")
                return None

            if not data:
                return None

            ext = _img_ext(real_url, resp.headers.get("content-type"))
            path = dest / f"img-{idx}{ext}"
            path.write_bytes(data)
            emit(f"    [url_transform] 已保存 #{idx}：{path.name}")
            return path

        return None


# ---------------------------------------------------------------------------
# Image: curl_cffi with TLS fingerprint impersonation (bypasses Cloudflare)
# ---------------------------------------------------------------------------

class CurlCffiImageStrategy:
    """Use ``curl_cffi`` to download images with Chrome TLS fingerprint
    impersonation, bypassing Cloudflare and similar anti-bot WAFs.

    This is a fallback for sites where plain httpx returns 403.
    """

    name = "curl_cffi_image"

    def match(self, url: str) -> float:
        # Only activate for http(s) URLs that look like direct image files
        if url.startswith(("http://", "https://")):
            ext = Path(urlparse(url).path).suffix.lower()
            if ext in _IMG_EXTS:
                return 0.5
        return 0.0

    def download(
        self, url: str, dest: Path, idx: int,
        emit: Callable = print, source_url: str | None = None, **kwargs,
    ) -> Path | None:
        safe = normalize_url(url)
        if not safe:
            emit(f"    [curl_cffi] 跳过无效地址 #{idx}：{(url or '')[:60]}")
            return None

        try:
            from curl_cffi import requests as curl_requests
        except ImportError:
            emit(f"    [curl_cffi] 未安装 curl_cffi，跳过 #{idx}")
            return None

        headers: dict[str, str] = {
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
        }
        if source_url:
            headers["Referer"] = source_url

        try:
            resp = curl_requests.get(
                safe, timeout=20, impersonate="chrome131",
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.content
        except Exception as e:
            emit(f"    [curl_cffi] 下载失败 #{idx}：{e}")
            return None

        if not data:
            return None

        ext = _img_ext(url, resp.headers.get("content-type"))
        path = dest / f"img-{idx}{ext}"
        path.write_bytes(data)
        emit(f"    [curl_cffi] 已保存 #{idx}：{path.name}")
        return path


# ---------------------------------------------------------------------------
# Image: fallback with UA rotation + retry (last resort)
# ---------------------------------------------------------------------------

class FallbackImageStrategy:
    """Last-resort image strategy: retries with rotated User-Agents and
    exponential backoff.  Tried only when all other strategies fail.

    Uses ``source_url`` as Referer (the article page) rather than the image
    origin, which helps with CDNs that require a cross-origin Referer.
    """

    name = "fallback_image"

    # URLs that are obviously fake / test data — skip immediately
    _FAKE_DOMAINS: set[str] = {"img.cdn", "cdn.example.com"}
    _FAKE_SUFFIXES: tuple[str, ...] = ("/a.png", "/b.jpg", "/a.jpg")

    def match(self, url: str) -> float:
        # Only match http(s) URLs with image extensions
        if not url.startswith(("http://", "https://")):
            return 0.0
        # Check fake domains early — don't even try
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if host in self._FAKE_DOMAINS:
            return 0.0
        if any(parsed.path.rstrip("/").endswith(s) for s in self._FAKE_SUFFIXES):
            if len(host) <= 2:  # single-letter or very short domain = fake
                return 0.0
        ext = Path(parsed.path).suffix.lower()
        return 0.1 if ext in _IMG_EXTS else 0.0

    def download(
        self, url: str, dest: Path, idx: int,
        emit: Callable = print, source_url: str | None = None, **kwargs,
    ) -> Path | None:
        safe = normalize_url(url)
        if not safe:
            emit(f"    [fallback] 跳过无效地址 #{idx}：{(url or '')[:60]}")
            return None

        referer = source_url or _origin(safe)

        for attempt, ua in enumerate(_FALLBACK_UAS):
            if attempt > 0:
                delay = 0.5 * (2 ** (attempt - 1))  # 0.5s, 1s, 2s, 4s
                emit(f"    [fallback] 重试 #{idx} (UA #{attempt + 1}, 等待 {delay:.0f}s)")
                time.sleep(delay)

            headers = {
                "User-Agent": ua,
                "Referer": referer,
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.9",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
                "Cache-Control": "no-cache",
            }

            try:
                resp = httpx.get(
                    safe, timeout=15.0, follow_redirects=True,
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.content
                if data:
                    ext = _img_ext(url, resp.headers.get("content-type"))
                    path = dest / f"img-{idx}{ext}"
                    path.write_bytes(data)
                    emit(f"    [fallback] 已保存 #{idx}：{path.name}")
                    return path
                emit(f"    [fallback] 空响应 #{idx} (UA #{attempt + 1})")
            except httpx.HTTPStatusError as e:
                emit(f"    [fallback] HTTP {e.response.status_code} #{idx} (UA #{attempt + 1})")
                continue
            except Exception as e:
                emit(f"    [fallback] 异常 #{idx} (UA #{attempt + 1})：{e}")
                continue

        emit(f"    [fallback] 所有 UA 均失败 #{idx}")
        return None


# ---------------------------------------------------------------------------
# Video: direct HTTP download
# ---------------------------------------------------------------------------

class HttpxDirectVideoStrategy:
    """Download video files directly via httpx (for self-hosted .mp4 etc.)."""

    name = "httpx_direct_video"

    def match(self, url: str) -> float:
        ext = Path(urlparse(url).path).suffix.lower()
        return 0.9 if ext in _DIRECT_VIDEO_EXTS else 0.0

    def download(
        self, url: str, dest: Path, idx: int,
        emit: Callable = print, **kwargs,
    ) -> Path | None:
        safe = normalize_url(url)
        if not safe:
            emit(f"    [httpx_direct_video] 跳过无效地址 #{idx}：{(url or '')[:60]}")
            return None

        ext = Path(urlparse(safe).path).suffix.lower()
        path = dest / f"vid-{idx}{ext}"
        try:
            with httpx.stream(
                "GET", safe, timeout=120.0, follow_redirects=True,
                headers={"User-Agent": _UA, "Referer": _origin(safe)},
            ) as r:
                r.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in r.iter_bytes(chunk_size=65536):
                        f.write(chunk)
        except Exception as e:
            emit(f"    [httpx_direct_video] 直链下载失败 #{idx}：{e}")
            return None

        emit(f"    [httpx_direct_video] 已保存 #{idx}：{path.name}")
        return path


# ---------------------------------------------------------------------------
# Video: yt-dlp for platform embeds
# ---------------------------------------------------------------------------

class YtDlpStrategy:
    """Download video via yt-dlp (handles YouTube, Bilibili, etc.).

    Extra keyword arguments:
      - ``max_seconds`` (int): only fetch first N seconds (default 60).
    """

    name = "yt_dlp"

    # URLs that strongly indicate yt-dlp is needed (embed platforms)
    _PLATFORM_HINTS = (
        "youtube.com", "youtu.be", "youtube-nocookie.com",
        "player.vimeo.com", "vimeo.com/video",
        "bilibili.com", "player.bilibili",
        "youku.com", "dailymotion.com",
        "wistia", "brightcove", "ixigua.com", "v.qq.com",
    )

    def match(self, url: str) -> float:
        lower = url.lower()
        if any(h in lower for h in self._PLATFORM_HINTS):
            return 0.9
        # Generic fallback — yt-dlp handles many URL schemes
        ext = Path(urlparse(url).path).suffix.lower()
        if ext in _DIRECT_VIDEO_EXTS:
            return 0.0  # direct download is better
        return 0.4  # weak match — try after direct strategies

    def download(
        self, url: str, dest: Path, idx: int,
        emit: Callable = print, **kwargs,
    ) -> Path | None:
        safe = normalize_url(url)
        if not safe:
            emit(f"    [yt_dlp] 跳过无效地址 #{idx}：{(url or '')[:60]}")
            return None

        base = _ytdlp_base()
        if base is None:
            emit(f"    [yt_dlp] 未安装 yt-dlp，跳过 #{idx}")
            return None

        max_seconds = kwargs.get("max_seconds", 60)
        emit(f"    [yt_dlp] 下载 #{idx}：{safe[:80]}")
        template = str(dest / f"vid-{idx}.%(ext)s")
        cmd = [
            *base, "--no-playlist", "--no-warnings",
            "--download-sections", f"*0-{max_seconds}",
            "-f", "best[ext=mp4]/best", "--merge-output-format", "mp4",
            "-o", template, safe,
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except (subprocess.SubprocessError, OSError) as e:
            emit(f"    [yt_dlp] 子进程异常 #{idx}：{e}")
            return None

        if r.returncode != 0:
            tail = (r.stderr or "").strip().splitlines()[-1:] or [""]
            emit(f"    [yt_dlp] 下载失败 #{idx}：{tail[0][:160]}")
            return None

        matches = sorted(dest.glob(f"vid-{idx}.*"))
        if matches:
            emit(f"    [yt_dlp] 已保存 #{idx}：{matches[0].name}")
            return matches[0]

        emit(f"    [yt_dlp] 下载后未找到文件 #{idx}")
        return None


