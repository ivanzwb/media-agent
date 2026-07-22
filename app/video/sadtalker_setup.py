"""One-click SadTalker setup: download models with resume support.

Models are downloaded to ``data/models/sadtalker/`` so the user never needs
to manually clone or configure SadTalker.  The download uses HTTP Range
headers for resume — re-running after a network drop picks up where it left
off without re-downloading completed files.

For the *packaged release*, the SadTalker source code (inference.py + src/)
is bundled alongside the app.  For *dev mode*, the code can come from a
local clone (``sadtalker_dir`` setting) **or** the bundled copy.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import shutil
import subprocess
import tarfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)

RUNTIME_TAG = "sadtalker-runtime-v1"
RUNTIME_ASSET = "sadtalker-runtime-win64-cuda118.tar.gz"
RUNTIME_MANIFEST = "sadtalker-runtime-win64-cuda118.manifest.json"
RUNTIME_RELEASE_BASE = (
    f"https://github.com/ivanzwb/release/releases/download/{RUNTIME_TAG}"
)

# ── Official release model manifest ──────────────────────────────────────

_RELEASE_BASE = (
    "https://github.com/OpenTalker/SadTalker/releases/download/v0.0.2-rc"
)

# (relative_path, expected_bytes_or_None) — files required for basic
# lip-sync.  GFPGAN face-enhancer weights are optional; they improve face
# quality but the pipeline works without them.
REQUIRED_FILES: list[tuple[str, int | None]] = [
    ("checkpoints/mapping_00109-model.pth.tar", 155_779_231),
    ("checkpoints/mapping_00229-model.pth.tar", 155_521_183),
    ("checkpoints/SadTalker_V0.0.2_256.safetensors", 725_066_984),
]

OPTIONAL_FILES: list[tuple[str, int | None]] = [
    ("gfpgan/weights/GFPGANv1.4.pth", None),
    ("gfpgan/weights/detection_Resnet50_Final.pth", None),
    ("gfpgan/weights/parsing_parsenet.pth", None),
]

ALL_FILES = REQUIRED_FILES + OPTIONAL_FILES

# ── SadTalker source code (bundled or external) ──────────────────────────

# Relative to app root — PyInstaller --add-data copies these into the
# packaged distribution so users don't need a git clone.
_BUNDLED_ST_REL = Path("sadtalker_src")  # inside data dir or next to exe


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

def _models_dir(data_dir: Path) -> Path:
    return data_dir / "models" / "sadtalker"


def _progress_path(data_dir: Path) -> Path:
    """JSON file tracking per-file download progress (byte offsets)."""
    return _models_dir(data_dir) / ".download_progress.json"


def runtime_dir(data_dir: Path) -> Path:
    """Managed, relocatable SadTalker runtime root."""
    return data_dir / "runtimes" / "sadtalker"


def runtime_python(data_dir: Path) -> Path:
    return runtime_dir(data_dir) / "python.exe"


def runtime_source(data_dir: Path) -> Path:
    return runtime_dir(data_dir) / "sadtalker-src"


def _runtime_version_path(data_dir: Path) -> Path:
    return runtime_dir(data_dir) / ".runtime-version"


def checkpoint_dir(data_dir: Path) -> Path:
    return _models_dir(data_dir) / "checkpoints"


def _runtime_archive(data_dir: Path) -> Path:
    return data_dir / "downloads" / RUNTIME_ASSET


def _runtime_url() -> str:
    return os.environ.get(
        "MEDIA_AGENT_SADTALKER_RUNTIME_MANIFEST_URL",
        f"{RUNTIME_RELEASE_BASE}/{RUNTIME_MANIFEST}",
    )


def _load_progress(data_dir: Path) -> dict[str, int]:
    p = _progress_path(data_dir)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_progress(data_dir: Path, prog: dict[str, int]) -> None:
    p = _progress_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(prog), encoding="utf-8")


def models_ready(data_dir: Path) -> bool:
    """True if every required model file has its exact expected size.

    A non-empty check is unsafe for resumable downloads: a truncated checkpoint
    (or even a tiny HTTP error body from the old, invalid URL) was previously
    treated as fully installed.
    """
    d = _models_dir(data_dir)
    return all(
        (d / f).is_file()
        and (d / f).stat().st_size >= (expected or 1)
        for f, expected in REQUIRED_FILES
    )


def _nvidia_smi() -> str | None:
    candidates = [
        shutil.which("nvidia-smi"),
        str(Path(os.environ.get("ProgramFiles", "C:/Program Files"))
            / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"),
        "C:/Windows/System32/nvidia-smi.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def gpu_status() -> dict[str, Any]:
    """Detect a usable NVIDIA driver without importing torch in the main app."""
    exe = _nvidia_smi()
    if not exe:
        return {
            "ok": False,
            "name": "",
            "reason": "未检测到 NVIDIA 显卡驱动（nvidia-smi）",
        }
    try:
        proc = subprocess.run(
            [exe, "--query-gpu=name,driver_version",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if proc.returncode != 0:
            return {
                "ok": False,
                "name": "",
                "reason": (proc.stderr or proc.stdout or
                           "nvidia-smi 执行失败").strip()[-300:],
            }
        first = (proc.stdout or "").strip().splitlines()[0]
        return {"ok": True, "name": first, "reason": ""}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "name": "", "reason": str(exc)}


def runtime_files_ready(data_dir: Path) -> bool:
    py = runtime_python(data_dir)
    src = runtime_source(data_dir)
    try:
        version_ok = (
            _runtime_version_path(data_dir).read_text(encoding="utf-8").strip()
            == RUNTIME_ASSET)
    except OSError:
        version_ok = False
    return py.is_file() and (src / "inference.py").is_file() and version_ok


_SMOKE_CACHE: dict[tuple[str, int, int, bool, bool], tuple[bool, str]] = {}


def _clear_smoke_cache() -> None:
    _SMOKE_CACHE.clear()


def _smoke_marker(data_dir: Path) -> Path:
    return runtime_dir(data_dir) / ".inference-smoke-ok.json"


def _smoke_fingerprint(data_dir: Path) -> dict[str, Any]:
    py = runtime_python(data_dir)
    return {
        "runtime": RUNTIME_ASSET,
        "python_size": py.stat().st_size if py.is_file() else 0,
        "models": {
            rel: (checkpoint_dir(data_dir) / Path(rel).name).stat().st_size
            if (checkpoint_dir(data_dir) / Path(rel).name).is_file() else 0
            for rel, _ in REQUIRED_FILES
        },
    }


def _deep_smoke_ready(data_dir: Path) -> bool:
    try:
        saved = json.loads(_smoke_marker(data_dir).read_text(encoding="utf-8"))
        return saved == _smoke_fingerprint(data_dir)
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def runtime_smoke(
    data_dir: Path, *, require_gpu: bool = True, deep: bool = False,
) -> tuple[bool, str]:
    """Verify imports/CUDA and optionally complete one real inference."""
    if not runtime_files_ready(data_dir):
        return False, "SadTalker runtime 文件不完整"
    if deep and _deep_smoke_ready(data_dir):
        return True, ""
    py = runtime_python(data_dir)
    source = runtime_source(data_dir)
    stat = py.stat()
    key = (str(py), stat.st_size, stat.st_mtime_ns, require_gpu, deep)
    if key in _SMOKE_CACHE:
        return _SMOKE_CACHE[key]
    code = (
        "import cv2, torch, torchvision, safetensors\n"
        "assert torch.cuda.is_available(), "
        "'CUDA unavailable in SadTalker runtime'\n"
        "print(torch.cuda.get_device_name(0))\n"
        if require_gpu else
        "import cv2, torch, torchvision, safetensors\nprint(torch.__version__)\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source)
    try:
        check = subprocess.run(
            [str(py), "-c", code],
            cwd=str(source), env=env, capture_output=True, text=True,
            timeout=90, check=False,
        )
        if check.returncode != 0:
            result = (False, (check.stderr or check.stdout or
                              "runtime import failed").strip()[-800:])
        else:
            help_check = subprocess.run(
                [str(py), str(source / "inference.py"), "--help"],
                cwd=str(source), env=env, capture_output=True, text=True,
                timeout=90, check=False,
            )
            if help_check.returncode != 0:
                result = (
                    False, (help_check.stderr or help_check.stdout or
                            "SadTalker import failed").strip()[-800:])
            elif deep:
                face = source / "examples" / "source_image" / "full_body_1.png"
                audio = source / "examples" / "driven_audio" / "bus_chinese.wav"
                if not face.is_file() or not audio.is_file():
                    result = (False, "runtime 缺少 SadTalker 推理验证素材")
                else:
                    result_dir = runtime_dir(data_dir) / ".smoke-result"
                    shutil.rmtree(result_dir, ignore_errors=True)
                    infer = subprocess.run(
                        [
                            str(py), str(source / "inference.py"),
                            "--driven_audio", str(audio),
                            "--source_image", str(face),
                            "--checkpoint_dir", str(checkpoint_dir(data_dir)),
                            "--result_dir", str(result_dir),
                            "--still", "--preprocess", "full",
                        ],
                        cwd=str(source), env=env, capture_output=True, text=True,
                        timeout=1200, check=False,
                    )
                    videos = list(result_dir.rglob("*.mp4"))
                    if infer.returncode == 0 and videos:
                        _smoke_marker(data_dir).write_text(
                            json.dumps(_smoke_fingerprint(data_dir), indent=2),
                            encoding="utf-8")
                        result = (True, "")
                    else:
                        result = (
                            False, (infer.stderr or infer.stdout or
                                    "SadTalker 未生成验证视频").strip()[-1200:])
                    shutil.rmtree(result_dir, ignore_errors=True)
            else:
                result = (True, "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = (False, str(exc))
    _SMOKE_CACHE[key] = result
    return result


def setup_status(data_dir: Path) -> dict[str, Any]:
    """Return complete readiness; model files alone are not installation."""
    d = _models_dir(data_dir)
    models_ok = models_ready(data_dir)
    runtime_ok = runtime_files_ready(data_dir)
    gpu = gpu_status()
    smoke_ok = False
    smoke_reason = ""
    if not gpu["ok"]:
        smoke_reason = gpu["reason"]
    elif runtime_ok and models_ok and _deep_smoke_ready(data_dir):
        smoke_ok, smoke_reason = runtime_smoke(data_dir)
    elif runtime_ok and models_ok:
        smoke_reason = "尚未完成 SadTalker 实际推理验证"
    elif not runtime_ok:
        smoke_reason = "尚未安装 SadTalker runtime"
    else:
        smoke_reason = "SadTalker 模型文件不完整"
    ready = bool(models_ok and runtime_ok and gpu["ok"] and smoke_ok)
    files_info: list[dict[str, Any]] = []
    for f, expected in ALL_FILES:
        fp = d / f
        exists = fp.is_file()
        size = fp.stat().st_size if exists else 0
        files_info.append({
            "path": f,
            "exists": exists,
            "size": size,
            "expected": expected,
            "ok": exists and (expected is None or size >= expected),
        })
    return {
        "ready": ready,
        "gpu_ok": gpu["ok"],
        "gpu_name": gpu["name"],
        "runtime_ok": runtime_ok,
        "models_ok": models_ok,
        "smoke_ok": smoke_ok,
        "reason": "" if ready else smoke_reason,
        "runtime_dir": str(runtime_dir(data_dir)),
        "models_dir": str(d),
        "files": files_info,
    }


# ---------------------------------------------------------------------------
# Download with resume
# ---------------------------------------------------------------------------

def _model_url(path: str, mirror: bool = True) -> str:
    """Official release asset URL.

    ``mirror`` is retained for API compatibility. The former hf-mirror URL was
    invalid (404); GitHub's release CDN supports Range and redirect following,
    which are both required by the resumable downloader.
    """
    del mirror
    return f"{_RELEASE_BASE}/{Path(path).name}"


def download_models(
    data_dir: Path,
    *,
    mirror: bool = True,
    proxy: str | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> bool:
    """Download *all* model files with per-file resume.

    Parameters
    ----------
    progress_cb:
        Called with ``(message, fraction)`` where *fraction* is 0.0-1.0 or
        -1 on error.  Intended for SSE streaming to the frontend.
    cancel_event:
        If set, the download loop exits early.

    Returns True when all *required* files are present.
    """
    dest = _models_dir(data_dir)
    dest.mkdir(parents=True, exist_ok=True)
    prog = _load_progress(data_dir)

    # Basic lip-sync needs the three official release assets. GFPGAN enhancer
    # files are optional and should not make installation slower or fail.
    download_files = REQUIRED_FILES
    total_bytes = sum(sz for _, sz in download_files if sz)
    downloaded_so_far = 0

    def _emit(msg: str, frac: float = 0.0) -> None:
        if progress_cb:
            try:
                progress_cb(msg, frac)
            except Exception:
                pass

    _emit("准备下载 SadTalker 模型…", 0.0)

    client = httpx.Client(
        # Release assets are large (~155MB + ~155MB + ~725MB). On slow or
        # cross-border routes there can be long gaps between chunks, so a
        # 60-second read timeout caused repeated false failures. Range resume
        # still protects genuine disconnects.
        timeout=httpx.Timeout(connect=60, read=300, write=30, pool=60),
        follow_redirects=True,
        proxy=proxy or None,
        headers={
            "User-Agent": "media-agent/1.0",
            # Byte ranges and exact-size validation require an identity stream.
            "Accept-Encoding": "identity",
        },
    )

    try:
        for idx, (rel, expected) in enumerate(download_files):
            if cancel_event and cancel_event.is_set():
                _emit("下载已取消", -1)
                return False

            fp = dest / rel
            fp.parent.mkdir(parents=True, exist_ok=True)

            # Skip if already fully downloaded
            existing = prog.get(rel, 0)
            if fp.is_file() and fp.stat().st_size > 0:
                # Clean up tiny error bodies left by the old invalid HF URLs.
                if fp.stat().st_size < 1024:
                    fp.unlink()
                    existing = 0
                    prog.pop(rel, None)
                elif expected and fp.stat().st_size >= expected:
                    downloaded_so_far += expected
                    continue
                else:
                    # Partial file — keep existing offset for resume
                    existing = fp.stat().st_size
            else:
                existing = 0
                prog.pop(rel, None)

            url = _model_url(rel, mirror=mirror)

            file_bytes = 0
            success = False
            # Retry in the same install operation. Each attempt re-reads the
            # partial file size and sends Range, so a dropped connection resumes
            # automatically instead of asking the user to click again.
            for attempt in range(1, 9):
                existing = fp.stat().st_size if fp.is_file() else 0
                try:
                    _emit(f"下载 {rel}…",
                          downloaded_so_far / max(total_bytes, 1))

                    # HEAD to get total size
                    try:
                        head = client.head(url)
                        # Exact release sizes are authoritative; a HEAD after
                        # CDN redirects may expose a transfer/content size that
                        # is not useful for completion validation.
                        remote_size = expected or int(
                            head.headers.get("content-length", 0))
                    except Exception:
                        remote_size = expected or 0

                    if existing > 0 and remote_size > 0 and existing >= remote_size:
                        file_bytes = remote_size
                        prog[rel] = file_bytes
                        _save_progress(data_dir, prog)
                        success = True
                        break

                    headers: dict[str, str] = {}
                    if existing > 0:
                        headers["Range"] = f"bytes={existing}-"
                        _emit(
                            f"续传 {rel}（{existing / 1048576:.1f} MB 已下载）…",
                            downloaded_so_far / max(total_bytes, 1))

                    with client.stream("GET", url, headers=headers) as resp:
                        if (resp.status_code == 416 and expected
                                and existing >= expected):
                            file_bytes = remote_size or existing
                            prog[rel] = file_bytes
                            _save_progress(data_dir, prog)
                            success = True
                            break

                        resp.raise_for_status()
                        mode = ("ab" if existing > 0
                                and resp.status_code == 206 else "wb")
                        bytes_written = existing if mode == "ab" else 0
                        last_emit_at = time.monotonic()
                        last_emit_bytes = bytes_written
                        with open(fp, mode) as fout:
                            for chunk in resp.iter_bytes(
                                    chunk_size=1024 * 256):
                                if cancel_event and cancel_event.is_set():
                                    _emit("下载已取消", -1)
                                    return False
                                fout.write(chunk)
                                bytes_written += len(chunk)
                                now = time.monotonic()
                                # One UI update per second: frequent enough to
                                # feel live without flooding the SSE stream.
                                if now - last_emit_at >= 1.0:
                                    interval = max(now - last_emit_at, 0.001)
                                    speed = (
                                        bytes_written - last_emit_bytes
                                    ) / interval
                                    file_total = remote_size or expected or 0
                                    size_text = (
                                        f"{bytes_written / 1048576:.1f}/"
                                        f"{file_total / 1048576:.1f} MB"
                                        if file_total
                                        else f"{bytes_written / 1048576:.1f} MB"
                                    )
                                    overall = (
                                        downloaded_so_far + bytes_written
                                    ) / total_bytes
                                    _emit(
                                        f"下载 {Path(rel).name}：{size_text} · "
                                        f"{speed / 1048576:.2f} MB/s",
                                        min(overall, 0.99))
                                    last_emit_at = now
                                    last_emit_bytes = bytes_written

                    file_bytes = bytes_written
                    prog[rel] = file_bytes
                    _save_progress(data_dir, prog)
                    _emit(
                        f"{Path(rel).name} 下载完成"
                        f"（{file_bytes / 1048576:.1f} MB）",
                        min((downloaded_so_far + file_bytes)
                            / max(total_bytes, 1), 0.99))
                    success = True
                    break
                except Exception as exc:
                    # Persist the exact on-disk offset before retrying.
                    current = fp.stat().st_size if fp.is_file() else 0
                    prog[rel] = current
                    _save_progress(data_dir, prog)
                    logger.warning(
                        "Download failed for %s (attempt %d/8): %s",
                        rel, attempt, exc)
                    if attempt < 8:
                        _emit(
                            f"下载连接中断，第 {attempt}/8 次自动重试"
                            f"（从 {current / 1048576:.1f} MB 续传）…",
                            downloaded_so_far / max(total_bytes, 1))
                        time.sleep(min(5, attempt))
                    else:
                        _emit(f"下载 {rel} 失败：{exc}", -1)

            if success:
                downloaded_so_far += file_bytes

    finally:
        client.close()

    ok = models_ready(data_dir)
    if ok:
        # Clean up progress file
        pp = _progress_path(data_dir)
        if pp.exists():
            pp.unlink()
        _emit("数字人模型安装完成！", 1.0)
    else:
        missing = [f for f, _ in REQUIRED_FILES
                   if not (dest / f).is_file() or (dest / f).stat().st_size == 0]
        _emit(f"部分模型下载失败：{', '.join(missing)}", -1)

    return ok


# ---------------------------------------------------------------------------
# Managed runtime download / extraction
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _download_runtime_part(
    client: httpx.Client,
    *,
    url: str,
    destination: Path,
    expected_size: int,
    expected_sha: str,
    completed_bytes: int,
    total_bytes: int,
    emit: Callable[[str, float], None],
    cancel_event: threading.Event | None,
) -> None:
    """Download one release-asset part, resuming an interrupted local file."""
    for attempt in range(1, 9):
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("安装已取消")
        existing = destination.stat().st_size if destination.is_file() else 0
        if existing == expected_size and _sha256(destination) == expected_sha:
            return
        if existing > expected_size:
            destination.unlink()
            existing = 0
        try:
            headers = {"Range": f"bytes={existing}-"} if existing else {}
            with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                append = existing > 0 and response.status_code == 206
                mode = "ab" if append else "wb"
                written = existing if append else 0
                last_at, last_bytes = time.monotonic(), written
                with destination.open(mode) as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        if cancel_event and cancel_event.is_set():
                            raise RuntimeError("安装已取消")
                        output.write(chunk)
                        written += len(chunk)
                        now = time.monotonic()
                        if now - last_at >= 1:
                            speed = (written - last_bytes) / max(
                                now - last_at, 0.001)
                            overall = (completed_bytes + written) / max(
                                total_bytes, 1)
                            emit(
                                "下载 CUDA runtime："
                                f"{(completed_bytes + written) / 1048576:.1f}"
                                f"/{total_bytes / 1048576:.1f} MB · "
                                f"{speed / 1048576:.2f} MB/s",
                                min(overall, 0.99),
                            )
                            last_at, last_bytes = now, written
            if destination.stat().st_size != expected_size:
                raise RuntimeError(
                    f"分片大小不正确：{destination.stat().st_size}/"
                    f"{expected_size}")
            if _sha256(destination) != expected_sha:
                destination.unlink(missing_ok=True)
                raise RuntimeError("分片 SHA-256 校验失败")
            return
        except Exception as exc:
            if attempt == 8:
                raise RuntimeError(
                    f"runtime 分片下载失败：{exc}") from exc
            current = destination.stat().st_size if destination.is_file() else 0
            emit(
                f"runtime 下载中断，第 {attempt}/8 次自动重试"
                f"（当前分片从 {current / 1048576:.1f} MB 续传）…",
                completed_bytes / max(total_bytes, 1),
            )
            time.sleep(min(attempt, 5))


def _download_runtime(
    data_dir: Path,
    *,
    proxy: str | None,
    progress_cb: Callable[[str, float], None] | None,
    cancel_event: threading.Event | None,
) -> Path:
    """Download a multipart release runtime with per-part Range resume."""
    archive = _runtime_archive(data_dir)
    archive.parent.mkdir(parents=True, exist_ok=True)
    manifest_url = _runtime_url()
    client = httpx.Client(
        timeout=httpx.Timeout(connect=60, read=300, write=30, pool=60),
        follow_redirects=True, proxy=proxy or None,
        headers={"User-Agent": "media-agent/1.0", "Accept-Encoding": "identity"},
    )

    def emit(message: str, fraction: float) -> None:
        if progress_cb:
            progress_cb(message, fraction)

    try:
        manifest_response = client.get(manifest_url)
        manifest_response.raise_for_status()
        manifest = manifest_response.json()
        parts = manifest.get("parts") or []
        total = int(manifest.get("archive_size") or 0)
        archive_sha = str(manifest.get("archive_sha256") or "").lower()
        if not parts or total <= 0 or len(archive_sha) != 64:
            raise RuntimeError("SadTalker runtime manifest 无效")
        base_url = manifest_url.rsplit("/", 1)[0]
        completed = 0
        local_parts: list[Path] = []
        for part in parts:
            name = Path(str(part["name"])).name
            size = int(part["size"])
            digest = str(part["sha256"]).lower()
            destination = archive.parent / name
            _download_runtime_part(
                client, url=f"{base_url}/{name}", destination=destination,
                expected_size=size, expected_sha=digest,
                completed_bytes=completed, total_bytes=total, emit=emit,
                cancel_event=cancel_event,
            )
            local_parts.append(destination)
            completed += size
        emit("合并并校验 SadTalker runtime…", 0.99)
        with archive.open("wb") as output:
            for part in local_parts:
                with part.open("rb") as source:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
        if archive.stat().st_size != total or _sha256(archive) != archive_sha:
            archive.unlink(missing_ok=True)
            raise RuntimeError("SadTalker runtime 合并后校验失败")
        return archive
    finally:
        client.close()


def _safe_extract(archive: Path, target: Path) -> None:
    target_abs = target.resolve()
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            member_path = (target / member.name).resolve()
            try:
                member_path.relative_to(target_abs)
            except ValueError as exc:
                raise RuntimeError(
                    f"runtime 压缩包包含不安全路径：{member.name}") from exc
        bundle.extractall(target)


def _install_runtime(
    data_dir: Path,
    *,
    proxy: str | None,
    progress_cb: Callable[[str, float], None] | None,
    cancel_event: threading.Event | None,
) -> None:
    if runtime_files_ready(data_dir):
        return
    archive = _download_runtime(
        data_dir, proxy=proxy, progress_cb=progress_cb,
        cancel_event=cancel_event)
    target = runtime_dir(data_dir)
    staging = target.with_name(target.name + ".installing")
    backup = target.with_name(target.name + ".previous")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    if progress_cb:
        progress_cb("解压 SadTalker CUDA runtime…", 0.99)
    _safe_extract(archive, staging)
    if not (staging / "python.exe").is_file():
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError("runtime 压缩包无效：缺少 python.exe")
    shutil.rmtree(backup, ignore_errors=True)
    if target.exists():
        target.replace(backup)
    staging.replace(target)
    try:
        _runtime_version_path(data_dir).write_text(
            RUNTIME_ASSET, encoding="utf-8")
        unpack = target / "Scripts" / "conda-unpack.exe"
        if unpack.is_file():
            proc = subprocess.run(
                [str(unpack)], cwd=str(target), capture_output=True, text=True,
                timeout=600, check=False)
            if proc.returncode != 0:
                raise RuntimeError(
                    "conda-unpack 失败：" +
                    (proc.stderr or proc.stdout or "")[-800:])
        if not runtime_files_ready(data_dir):
            raise RuntimeError("runtime 解压后文件不完整")
        shutil.rmtree(backup, ignore_errors=True)
        archive.unlink(missing_ok=True)
        for part in archive.parent.glob(f"{RUNTIME_ASSET}.part*"):
            part.unlink(missing_ok=True)
        _clear_smoke_cache()
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        if backup.exists():
            backup.replace(target)
        raise


# ---------------------------------------------------------------------------
# Managed source / interpreter resolution
# ---------------------------------------------------------------------------


def resolve_sadtalker_dir(data_dir: Path) -> str | None:
    if not runtime_files_ready(data_dir):
        return None
    source = runtime_source(data_dir)
    return str(source)


def resolve_sadtalker_python(data_dir: Path) -> str | None:
    if not runtime_files_ready(data_dir):
        return None
    python = runtime_python(data_dir)
    return str(python)


# ---------------------------------------------------------------------------
# Full setup orchestration (called from API endpoint)
# ---------------------------------------------------------------------------

def run_setup(
    data_dir: Path,
    *,
    mirror: bool = True,
    proxy: str | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run the full SadTalker setup: download models + resolve paths.

    Returns a dict suitable for JSON serialisation with keys:
    ``ok``, ``message``, ``sadtalker_dir``, ``sadtalker_python``.
    """
    def _emit(msg: str, frac: float = 0.0) -> None:
        if progress_cb:
            try:
                progress_cb(msg, frac)
            except Exception:
                pass

    _emit("检查 NVIDIA GPU…", 0.0)
    gpu = gpu_status()
    if not gpu["ok"]:
        return {"ok": False, "message": gpu["reason"], **setup_status(data_dir)}

    # Runtime is deliberately separate from the ~1 GB model payload so either
    # can be resumed/repaired without re-downloading the other.
    try:
        _install_runtime(
            data_dir, proxy=proxy,
            progress_cb=(
                (lambda message, fraction:
                 _emit(message, min(max(fraction, 0.0) * 0.45, 0.45)))
                if progress_cb else None
            ),
            cancel_event=cancel_event,
        )
    except Exception as exc:
        return {"ok": False, "message": str(exc), **setup_status(data_dir)}

    def model_progress(message: str, fraction: float) -> None:
        _emit(message, fraction if fraction < 0 else 0.45 + fraction * 0.5)

    ok = download_models(
        data_dir, mirror=mirror, proxy=proxy,
        progress_cb=model_progress, cancel_event=cancel_event)
    if not ok:
        return {"ok": False, "message": "模型下载失败，请检查网络后重试",
                **setup_status(data_dir)}

    _emit("运行 SadTalker 实际推理验证（首次安装可能需要数分钟）…", 0.97)
    _clear_smoke_cache()
    smoke_ok, reason = runtime_smoke(data_dir, deep=True)
    if not smoke_ok:
        return {"ok": False, "message": f"runtime 验证失败：{reason}",
                **setup_status(data_dir)}

    status = setup_status(data_dir)
    _emit("数字人 runtime 与模型安装完成！", 1.0)
    return {
        **status,
        "ok": status["ready"],
        "message": ("数字人已就绪！" if status["ready"]
                    else status["reason"] or "数字人安装不完整"),
        "sadtalker_dir": resolve_sadtalker_dir(data_dir) or "",
        "sadtalker_python": resolve_sadtalker_python(data_dir) or "",
    }
