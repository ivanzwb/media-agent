"""Platform contract shared by managed ML runtimes."""
from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePlatform:
    key: str
    system: str
    arch: str
    accelerator: str
    requires_gpu: bool
    python_relpath: Path
    conda_unpack_relpath: Path
    slow: bool = False

    @property
    def device(self) -> str:
        return "cuda" if self.accelerator == "CUDA" else "cpu"


def detect_runtime_platform(
    system: str | None = None,
    machine: str | None = None,
) -> RuntimePlatform | None:
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    if system == "windows" and machine in {"amd64", "x86_64"}:
        return RuntimePlatform(
            "windows-cuda", "windows", "x64", "CUDA", True,
            Path("python.exe"), Path("Scripts/conda-unpack.exe"))
    if system == "darwin" and machine in {"arm64", "aarch64"}:
        return RuntimePlatform(
            "macos-arm64-cpu", "macos", "arm64", "CPU", False,
            Path("bin/python"), Path("bin/conda-unpack"), slow=True)
    if system == "darwin" and machine in {"x86_64", "amd64"}:
        return RuntimePlatform(
            "macos-x64-cpu", "macos", "x64", "CPU", False,
            Path("bin/python"), Path("bin/conda-unpack"), slow=True)
    return None


def runtime_asset(kind: str, target: RuntimePlatform) -> str:
    if target.key == "windows-cuda":
        suffix = "win64-cuda118" if kind == "sadtalker" else "win64-cuda128"
    else:
        suffix = f"macos-{'arm64' if target.arch == 'arm64' else 'x64'}-cpu"
    return f"{kind}-runtime-{suffix}.tar.gz"


def runtime_manifest(kind: str, target: RuntimePlatform) -> str:
    return runtime_asset(kind, target).removesuffix(
        ".tar.gz") + ".manifest.json"


def prepare_runtime_tree(root: Path, target: RuntimePlatform) -> None:
    """Make a conda-pack payload executable and usable after extraction."""
    for relative in (target.python_relpath, target.conda_unpack_relpath):
        executable = root / relative
        if executable.is_file() and target.system != "windows":
            executable.chmod(executable.stat().st_mode | 0o111)
    if target.system == "macos":
        # Browser/app downloads may carry quarantine onto extracted Mach-O
        # binaries. Failure is harmless on filesystems without xattrs.
        subprocess.run(
            ["xattr", "-dr", "com.apple.quarantine", str(root)],
            capture_output=True, text=True, timeout=60, check=False)
