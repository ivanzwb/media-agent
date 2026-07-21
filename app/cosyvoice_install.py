"""One-click installer for the prebuilt CosyVoice runtime.

Replaces the manual ``setup-optional`` script: downloads the conda-pack'd
runtime asset from the release, extracts it, finalizes conda-pack relocation,
and (optionally) downloads the CosyVoice2 model — all with progress callbacks
so the Settings UI can show a progress bar. Nothing is compiled on the user's
machine.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# Progress callback: (message, percent|None). percent None → indeterminate step.
Progress = Callable[[str, "int | None"], None]

# Per-platform runtime asset (only Windows is built today).
_RUNTIME_ASSET = {
    "win32": "cosyvoice-runtime-win64.tar.gz",
}
_RELEASE_BASE = os.environ.get(
    "MEDIA_AGENT_RELEASE_BASE",
    "https://github.com/ivanzwb/release/releases/latest/download")
_MODEL_ID = "FunAudioLLM/CosyVoice2-0.5B"


def _install_root() -> Path:
    """Where the runtime + models live (next to the exe when frozen, else cwd)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def runtime_target() -> Path:
    return _install_root() / "_cosyvoice-runtime"


def model_target() -> Path:
    return _install_root() / "pretrained_models" / "CosyVoice2-0.5B"


def asset_name() -> str | None:
    return _RUNTIME_ASSET.get(sys.platform)


def is_supported() -> bool:
    return asset_name() is not None


def _runtime_python(target: Path) -> Path | None:
    for c in (target / "python.exe", target / "bin" / "python",
              target / "bin" / "python3"):
        if c.exists():
            return c
    return None


def _download(url: str, dest: Path, progress: Progress, label: str) -> None:
    import httpx
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=None) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0) or 0)
        done = 0
        last_pct = -1
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(262144):
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = int(done * 100 / total)
                    if pct != last_pct:
                        last_pct = pct
                        progress(f"{label} {done // 1048576}/{total // 1048576} MB",
                                 pct)
                elif done % (8 * 1048576) < 262144:
                    progress(f"{label} {done // 1048576} MB", None)
    os.replace(tmp, dest)


def _extract(tgz: Path, target: Path, progress: Progress) -> None:
    progress("解压运行时…", None)
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tgz, "r:gz") as tf:
        try:
            tf.extractall(target, filter="data")  # py3.12+ safe extraction
        except TypeError:
            tf.extractall(target)


def _conda_unpack(target: Path, progress: Progress) -> None:
    progress("修复运行时路径（conda-unpack）…", None)
    win_exe = target / "Scripts" / "conda-unpack.exe"
    if win_exe.exists():
        subprocess.run([str(win_exe)], cwd=str(target), capture_output=True)
        return
    nix = target / "bin" / "conda-unpack"
    if nix.exists():
        subprocess.run([str(nix)], cwd=str(target), capture_output=True)
        return
    py = _runtime_python(target)
    if py:
        subprocess.run([str(py), "-m", "conda_pack.scripts.conda_unpack"],
                       cwd=str(target), capture_output=True)


def _download_model(target: Path, progress: Progress) -> None:
    model_dir = model_target()
    if model_dir.exists() and any(model_dir.iterdir()):
        progress("模型已存在，跳过下载", 100)
        return
    py = _runtime_python(target)
    if not py:
        raise RuntimeError("运行时 python 缺失，无法下载模型")
    model_dir.mkdir(parents=True, exist_ok=True)
    progress("下载 CosyVoice 模型（约 1.5GB，首次）…", None)
    env = os.environ.copy()
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    env["HF_HUB_DISABLE_XET"] = "1"
    env["HF_XET_DISABLE"] = "1"
    code = (
        "from huggingface_hub import snapshot_download; "
        f"snapshot_download('{_MODEL_ID}', local_dir=r'{model_dir}', "
        "max_workers=2)")
    proc = subprocess.run([str(py), "-c", code], env=env,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("模型下载失败：" + (proc.stderr or "")[-400:])
    progress("模型下载完成", 100)


def install_runtime(progress: Progress, want_model: bool = True) -> None:
    """Full install: runtime asset (+ optional model). Raises on failure."""
    name = asset_name()
    if not name:
        raise RuntimeError(
            f"当前系统（{sys.platform}）暂无预编译 CosyVoice 运行时，"
            "请在有 NVIDIA/CPU 支持的 Windows 上使用，或改用 Kitten 配音。")
    target = runtime_target()
    if _runtime_python(target):
        progress("运行时已安装", 100)
    else:
        tgz = _install_root() / name
        progress("开始下载预编译运行时…", 0)
        _download(f"{_RELEASE_BASE}/{name}", tgz, progress, "下载运行时")
        _extract(tgz, target, progress)
        try:
            tgz.unlink()
        except OSError:
            pass
        _conda_unpack(target, progress)
        if not _runtime_python(target):
            raise RuntimeError("运行时安装失败（未找到 python）")
        progress("运行时安装完成", 100)
    if want_model:
        _download_model(target, progress)
    progress("CosyVoice 安装完成，可在语音里选择 CosyVoice", 100)
