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
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)

# ── HuggingFace repo & model manifest ────────────────────────────────────

_HF_REPO = "vinthony/SadTalker"
_HF_MIRROR = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")

# (relative_path, expected_bytes_or_None) — files required for basic
# lip-sync.  GFPGAN face-enhancer weights are optional; they improve face
# quality but the pipeline works without them.
REQUIRED_FILES: list[tuple[str, int | None]] = [
    ("checkpoints/mapping_00109-model.pth.tar", 36_406_823),
    ("checkpoints/mapping_00229-model.pth.tar", 36_406_823),
    ("checkpoints/SadTalker_V0.0.2_256.safetensors", None),
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
    """True if every *required* model file exists and is non-empty."""
    d = _models_dir(data_dir)
    return all((d / f).is_file() and (d / f).stat().st_size > 0
               for f, _ in REQUIRED_FILES)


def setup_status(data_dir: Path) -> dict[str, Any]:
    """Return a JSON-serialisable status dict for the frontend."""
    d = _models_dir(data_dir)
    ready = models_ready(data_dir)
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
        "models_dir": str(d),
        "files": files_info,
    }


# ---------------------------------------------------------------------------
# Download with resume
# ---------------------------------------------------------------------------

def _hf_url(path: str, mirror: bool = True) -> str:
    base = _HF_MIRROR if mirror else "https://huggingface.co"
    return f"{base}/{_HF_REPO}/resolve/main/{path}"


def download_models(
    data_dir: Path,
    *,
    mirror: bool = True,
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

    total_bytes = sum(sz for _, sz in ALL_FILES if sz)
    downloaded_so_far = 0

    def _emit(msg: str, frac: float = 0.0) -> None:
        if progress_cb:
            try:
                progress_cb(msg, frac)
            except Exception:
                pass

    _emit("准备下载 SadTalker 模型…", 0.0)

    client = httpx.Client(
        timeout=httpx.Timeout(connect=30, read=60, write=10, pool=30),
        follow_redirects=True,
        headers={"User-Agent": "media-agent/1.0"},
    )

    try:
        for idx, (rel, expected) in enumerate(ALL_FILES):
            if cancel_event and cancel_event.is_set():
                _emit("下载已取消", -1)
                return False

            fp = dest / rel
            fp.parent.mkdir(parents=True, exist_ok=True)

            # Skip if already fully downloaded
            existing = prog.get(rel, 0)
            if fp.is_file() and fp.stat().st_size > 0:
                if expected and fp.stat().st_size >= expected:
                    downloaded_so_far += expected or fp.stat().st_size
                    continue
                # Partial file — keep existing offset for resume
                existing = fp.stat().st_size
            else:
                existing = 0
                prog.pop(rel, None)

            url = _hf_url(rel, mirror=mirror)

            try:
                _emit(f"下载 {rel}…", downloaded_so_far / max(total_bytes, 1))

                # HEAD to get total size
                try:
                    head = client.head(url)
                    remote_size = int(head.headers.get("content-length", 0))
                except Exception:
                    remote_size = expected or 0

                if existing > 0 and remote_size > 0 and existing >= remote_size:
                    # File completed between checks
                    prog[rel] = remote_size
                    _save_progress(data_dir, prog)
                    downloaded_so_far += remote_size
                    continue

                headers: dict[str, str] = {}
                if existing > 0:
                    headers["Range"] = f"bytes={existing}-"
                    _emit(f"续传 {rel}（{existing / 1048576:.1f} MB 已下载）…",
                           downloaded_so_far / max(total_bytes, 1))

                with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code == 416:
                        # Range not satisfiable → file complete
                        prog[rel] = remote_size or existing
                        _save_progress(data_dir, prog)
                        downloaded_so_far += remote_size or existing
                        continue

                    resp.raise_for_status()

                    mode = "ab" if existing > 0 and resp.status_code == 206 else "wb"
                    bytes_written = existing if mode == "ab" else 0
                    with open(fp, mode) as fout:
                        for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                            if cancel_event and cancel_event.is_set():
                                fout.close()
                                _emit("下载已取消", -1)
                                return False
                            fout.write(chunk)
                            bytes_written += len(chunk)
                            if total_bytes > 0:
                                overall = (downloaded_so_far + bytes_written) / total_bytes
                                _emit(f"下载 {rel}…",
                                       min(overall, 0.99))

                    prog[rel] = bytes_written
                    _save_progress(data_dir, prog)
                    downloaded_so_far += bytes_written

            except Exception as exc:
                logger.warning("Download failed for %s: %s", rel, exc)
                _emit(f"下载 {rel} 失败：{exc}", -1)
                # Continue with next file; required check at the end

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
# Source code resolution
# ---------------------------------------------------------------------------

def resolve_sadtalker_dir(data_dir: Path) -> str | None:
    """Find the SadTalker source code directory.

    Priority:
    1. User-configured ``sadtalker_dir`` (from DB / env)
    2. Bundled copy inside the data dir
    3. Common development locations
    """
    # 1. Bundled copy next to data dir (packaged release)
    bundled = data_dir / "sadtalker_src"
    if (bundled / "inference.py").is_file():
        return str(bundled)

    # 2. Common dev locations
    candidates = [
        Path("C:/Projects/SadTalker"),
        Path.home() / "SadTalker",
        Path.cwd() / "SadTalker",
    ]
    for c in candidates:
        if (c / "inference.py").is_file():
            return str(c)

    return None


def resolve_sadtalker_python(data_dir: Path) -> str | None:
    """Find a suitable Python executable for SadTalker.

    Priority:
    1. User-configured ``sadtalker_python``
    2. The CosyVoice sidecar Python (if present)
    3. System ``python``
    """
    # CosyVoice sidecar (packaged release)
    candidates = [
        Path("C:/Projects/media-agent/_cosyvoice-python/python.exe"),
        Path("_cosyvoice-python/python.exe"),
    ]
    for c in candidates:
        if c.is_file():
            return str(c)

    return None  # let caller fall back to "python"


# ---------------------------------------------------------------------------
# Full setup orchestration (called from API endpoint)
# ---------------------------------------------------------------------------

def run_setup(
    data_dir: Path,
    *,
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

    _emit("开始安装数字人组件…", 0.0)

    # Step 1: Download models
    ok = download_models(data_dir, progress_cb=progress_cb,
                         cancel_event=cancel_event)
    if not ok:
        return {"ok": False, "message": "模型下载失败，请检查网络后重试"}

    # Step 2: Resolve SadTalker directory
    st_dir = resolve_sadtalker_dir(data_dir)
    if not st_dir:
        return {
            "ok": False,
            "message": "模型已下载，但未找到 SadTalker 代码。"
                       "请在「SadTalker 目录」中手动指定路径。",
        }

    st_python = resolve_sadtalker_python(data_dir)

    _emit("数字人安装完成！", 1.0)
    return {
        "ok": True,
        "message": "数字人已就绪！",
        "sadtalker_dir": st_dir,
        "sadtalker_python": st_python or "",
    }
