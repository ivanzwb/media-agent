"""Managed CosyVoice runtime and model installation.

Release and development builds install the same relocatable archive under
``data/runtimes/cosyvoice``.  Models live separately under
``data/models/cosyvoice/CosyVoice2-0.5B`` so either payload can be repaired or
upgraded without asking users for Python, source, or model paths.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from app.runtime_platform import (
    RuntimePlatform, detect_runtime_platform, prepare_runtime_tree,
    runtime_asset, runtime_manifest)

RUNTIME_TAG = "cosyvoice-runtime-v1"
RUNTIME_ASSET = "cosyvoice-runtime-win64-cuda121.tar.gz"
RUNTIME_MANIFEST = "cosyvoice-runtime-win64-cuda121.manifest.json"
RUNTIME_RELEASE_BASE = (
    f"https://github.com/ivanzwb/release/releases/download/{RUNTIME_TAG}")
MODEL_REPO = "FunAudioLLM/CosyVoice2-0.5B"
MODEL_NAME = "CosyVoice2-0.5B"
MODEL_REQUIRED = (
    "cosyvoice2.yaml", "llm.pt", "flow.pt", "hift.pt",
    "speech_tokenizer_v2.onnx",
)

Progress = Callable[[str, float], None]
_SMOKE_CACHE: dict[tuple[str, int, bool], tuple[bool, str]] = {}


def runtime_target(system: str | None = None,
                   machine: str | None = None) -> RuntimePlatform | None:
    return detect_runtime_platform(
        system or platform.system(), machine or platform.machine())


def _unsupported_reason(target: RuntimePlatform | None = None) -> str:
    return (
        f"当前平台没有可用的 CosyVoice managed runtime 资产"
        f"（{platform.system()} {platform.machine()}）")


def runtime_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "runtimes" / "cosyvoice"


def runtime_python(data_dir: Path) -> Path:
    target = runtime_target()
    relative = target.python_relpath if target else Path("python.exe")
    return runtime_dir(data_dir) / relative


def runtime_worker(data_dir: Path) -> Path:
    return runtime_dir(data_dir) / "cosyvoice-worker.py"


def runtime_source(data_dir: Path) -> Path:
    return runtime_dir(data_dir) / "cosyvoice-src"


def model_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "models" / "cosyvoice" / MODEL_NAME


def _version_path(data_dir: Path) -> Path:
    return runtime_dir(data_dir) / ".runtime-version"


def _smoke_path(data_dir: Path) -> Path:
    return runtime_dir(data_dir) / ".model-smoke-ok.json"


def _archive_path(data_dir: Path) -> Path:
    target = runtime_target()
    asset = runtime_asset("cosyvoice", target) if target else RUNTIME_ASSET
    return Path(data_dir) / "downloads" / asset


def _manifest_url() -> str:
    target = runtime_target()
    manifest = runtime_manifest("cosyvoice", target) if target else RUNTIME_MANIFEST
    custom = os.environ.get(
        "MEDIA_AGENT_COSYVOICE_RUNTIME_MANIFEST_URL", "").strip()
    if custom:
        return custom
    official = f"{RUNTIME_RELEASE_BASE}/{manifest}"
    gh_mirror = os.environ.get("MEDIA_AGENT_GITHUB_MIRROR", "").strip()
    if gh_mirror:
        return f"{gh_mirror.rstrip('/')}/{official}"
    return official


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def runtime_files_ready(data_dir: Path) -> bool:
    target = runtime_target()
    expected_asset = (
        runtime_asset("cosyvoice", target) if target else RUNTIME_ASSET)
    try:
        version = _version_path(data_dir).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return (
        version == expected_asset
        and runtime_python(data_dir).is_file()
        and runtime_worker(data_dir).is_file()
        and (runtime_source(data_dir) / "cosyvoice" / "cli" /
             "cosyvoice.py").is_file()
    )


def _model_manifest(data_dir: Path) -> dict[str, Any] | None:
    path = model_dir(data_dir) / ".model-ready.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def models_ready(data_dir: Path) -> bool:
    root = model_dir(data_dir)
    manifest = _model_manifest(data_dir)
    if not manifest or manifest.get("repo") != MODEL_REPO:
        return False
    files = manifest.get("files")
    if not isinstance(files, dict):
        return False
    for relative in MODEL_REQUIRED:
        path = root / relative
        expected = files.get(relative)
        if not path.is_file() or not isinstance(expected, int) or expected <= 0:
            return False
        if path.stat().st_size != expected:
            return False
    return True


def _fingerprint(data_dir: Path) -> dict[str, Any]:
    target = runtime_target()
    return {
        "runtime": (
            runtime_asset("cosyvoice", target) if target else RUNTIME_ASSET),
        "model_repo": MODEL_REPO,
        "files": {
            name: (model_dir(data_dir) / name).stat().st_size
            if (model_dir(data_dir) / name).is_file() else 0
            for name in MODEL_REQUIRED
        },
    }


def _deep_smoke_ready(data_dir: Path) -> bool:
    try:
        return json.loads(_smoke_path(data_dir).read_text(
            encoding="utf-8")) == _fingerprint(data_dir)
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def runtime_smoke(data_dir: Path, *, deep: bool = False,
                  timeout: int = 1200) -> tuple[bool, str]:
    if not runtime_files_ready(data_dir):
        return False, "CosyVoice runtime 文件不完整"
    if not models_ready(data_dir):
        return False, "CosyVoice2 模型文件不完整"
    if deep and _deep_smoke_ready(data_dir):
        return True, ""
    py = runtime_python(data_dir)
    key = (str(py), py.stat().st_mtime_ns, deep)
    if key in _SMOKE_CACHE:
        return _SMOKE_CACHE[key]
    command = [
        str(py), str(runtime_worker(data_dir)), "smoke",
        "--model-dir", str(model_dir(data_dir)),
    ]
    if deep:
        command.append("--deep")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(runtime_source(data_dir))
    target = runtime_target()
    if target is not None and not target.requires_gpu:
        env["MEDIA_AGENT_TORCH_DEVICE"] = "cpu"
        timeout = max(timeout, 3600)
    try:
        proc = subprocess.run(
            command, cwd=str(runtime_dir(data_dir)), env=env,
            capture_output=True, text=True, timeout=timeout, check=False)
        if proc.returncode:
            result = (False, (proc.stderr or proc.stdout or
                              "CosyVoice smoke failed").strip()[-1600:])
        else:
            if deep:
                _smoke_path(data_dir).write_text(
                    json.dumps(_fingerprint(data_dir), indent=2),
                    encoding="utf-8")
            result = (True, "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = (False, str(exc))
    _SMOKE_CACHE[key] = result
    return result


def setup_status(data_dir: Path) -> dict[str, Any]:
    from app.video.sadtalker_setup import gpu_status

    target = runtime_target()
    runtime_ok = runtime_files_ready(data_dir)
    models_ok = models_ready(data_dir)
    gpu = gpu_status() if target is not None and target.requires_gpu else {
        "ok": False, "name": "", "reason": ""}
    hardware_ok = bool(
        target is not None and (not target.requires_gpu or gpu["ok"]))
    asset_available = target is not None
    installable = bool(asset_available and hardware_ok)
    smoke_ok = bool(
        runtime_ok and models_ok and hardware_ok and _deep_smoke_ready(data_dir))
    if target is None or (not asset_available and not runtime_ok):
        reason = _unsupported_reason(target)
    elif not hardware_ok:
        reason = gpu["reason"]
    elif not runtime_ok:
        reason = "尚未安装 CosyVoice runtime"
    elif not models_ok:
        reason = "CosyVoice2 模型文件不完整"
    elif not smoke_ok:
        reason = "尚未完成 CosyVoice 模型加载验证"
    else:
        reason = ""
    return {
        "ready": bool(runtime_ok and models_ok and hardware_ok and smoke_ok),
        "supported": target is not None,
        "asset_available": asset_available,
        "installable": installable,
        "platform": target.key if target is not None else "unsupported",
        "accelerator": target.accelerator if target is not None else "",
        "device_ok": hardware_ok,
        "device_name": gpu["name"] if target is not None and target.requires_gpu
        else (platform.machine() if target is not None else ""),
        "performance_warning": (
            "macOS 使用 CPU 推理，声音合成速度可能较慢"
            if target is not None and target.accelerator == "CPU" else ""),
        "gpu_ok": bool(gpu["ok"]),
        "gpu_name": gpu["name"],
        "runtime_ok": runtime_ok,
        "models_ok": models_ok,
        "smoke_ok": smoke_ok,
        "reason": reason,
        "runtime_dir": str(runtime_dir(data_dir)),
        "models_dir": str(model_dir(data_dir)),
    }


def _emit(callback: Progress | None, message: str, fraction: float) -> None:
    if callback:
        try:
            callback(message, fraction)
        except Exception:
            pass


def _download_part(
    client: httpx.Client, *, url: str, destination: Path,
    expected_size: int, expected_sha: str, completed: int, total: int,
    progress_cb: Progress | None, cancel_event: threading.Event | None,
) -> None:
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
                written = existing if append else 0
                last_at, last_bytes = time.monotonic(), written
                with destination.open("ab" if append else "wb") as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        if cancel_event and cancel_event.is_set():
                            raise RuntimeError("安装已取消")
                        output.write(chunk)
                        written += len(chunk)
                        now = time.monotonic()
                        if now - last_at >= 1:
                            speed = (written - last_bytes) / max(
                                now - last_at, 0.001)
                            _emit(
                                progress_cb,
                                f"下载 CosyVoice runtime："
                                f"{(completed + written) / 1048576:.1f}/"
                                f"{total / 1048576:.1f} MB · "
                                f"{speed / 1048576:.2f} MB/s",
                                min((completed + written) / max(total, 1), .99))
                            last_at, last_bytes = now, written
            if destination.stat().st_size != expected_size:
                raise RuntimeError("runtime 分片大小不正确")
            if _sha256(destination) != expected_sha:
                destination.unlink(missing_ok=True)
                raise RuntimeError("runtime 分片 SHA-256 校验失败")
            return
        except Exception as exc:
            if attempt == 8:
                raise RuntimeError(f"runtime 分片下载失败：{exc}") from exc
            current = destination.stat().st_size if destination.exists() else 0
            _emit(progress_cb, f"runtime 下载中断，第 {attempt}/8 次自动重试"
                  f"（从 {current / 1048576:.1f} MB 续传）…",
                  completed / max(total, 1))
            time.sleep(min(attempt, 5))


def _download_runtime(data_dir: Path, *, proxy: str | None,
                      progress_cb: Progress | None,
                      cancel_event: threading.Event | None) -> Path:
    archive = _archive_path(data_dir)
    archive.parent.mkdir(parents=True, exist_ok=True)
    manifest_url = _manifest_url()
    client = httpx.Client(
        timeout=httpx.Timeout(connect=60, read=300, write=30, pool=60),
        follow_redirects=True, proxy=proxy or None,
        headers={"User-Agent": "media-agent/1.0",
                 "Accept-Encoding": "identity"})
    try:
        response = client.get(manifest_url)
        response.raise_for_status()
        manifest = response.json()
        parts = manifest.get("parts") or []
        total = int(manifest.get("archive_size") or 0)
        archive_sha = str(manifest.get("archive_sha256") or "").lower()
        if not parts or total <= 0 or len(archive_sha) != 64:
            raise RuntimeError("CosyVoice runtime manifest 无效")
        base = manifest_url.rsplit("/", 1)[0]
        local_parts: list[Path] = []
        completed = 0
        for part in parts:
            name = Path(str(part["name"])).name
            destination = archive.parent / name
            _download_part(
                client, url=f"{base}/{name}", destination=destination,
                expected_size=int(part["size"]),
                expected_sha=str(part["sha256"]).lower(),
                completed=completed, total=total, progress_cb=progress_cb,
                cancel_event=cancel_event)
            local_parts.append(destination)
            completed += int(part["size"])
        _emit(progress_cb, "合并并校验 CosyVoice runtime…", .99)
        with archive.open("wb") as output:
            for part in local_parts:
                with part.open("rb") as source:
                    shutil.copyfileobj(source, output, 1024 * 1024)
        if archive.stat().st_size != total or _sha256(archive) != archive_sha:
            archive.unlink(missing_ok=True)
            raise RuntimeError("CosyVoice runtime 合并后校验失败")
        return archive
    finally:
        client.close()


def _safe_extract(archive: Path, target: Path) -> None:
    root = target.resolve()
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            try:
                (target / member.name).resolve().relative_to(root)
            except ValueError as exc:
                raise RuntimeError(
                    f"runtime 压缩包包含不安全路径：{member.name}") from exc
            if member.issym() or member.islnk():
                link = Path(member.name).parent / member.linkname
                try:
                    (target / link).resolve().relative_to(root)
                except ValueError as exc:
                    raise RuntimeError(
                        f"runtime 压缩包包含不安全链接：{member.name}") from exc
        bundle.extractall(target)


def _replace(source: Path, target: Path) -> None:
    for attempt in range(8):
        try:
            source.replace(target)
            return
        except OSError:
            if attempt == 7:
                raise
            time.sleep(.25 * (2 ** attempt))


def install_runtime_archive(data_dir: Path, archive: Path, *,
                            progress_cb: Progress | None = None,
                            cleanup_archive: bool = False) -> None:
    archive = Path(archive).resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"CosyVoice runtime archive 不存在：{archive}")
    target = runtime_dir(data_dir)
    staging = target.with_name(target.name + ".installing")
    backup = target.with_name(target.name + ".previous")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    _emit(progress_cb, "解压 CosyVoice runtime…", .99)
    _safe_extract(archive, staging)
    target_spec = runtime_target()
    python_relative = (
        target_spec.python_relpath if target_spec else Path("python.exe"))
    required = (staging / python_relative, staging / "cosyvoice-worker.py")
    if not all(path.is_file() for path in required):
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError("runtime 压缩包无效")
    shutil.rmtree(backup, ignore_errors=True)
    if target.exists():
        _replace(target, backup)
    _replace(staging, target)
    try:
        asset = (
            runtime_asset("cosyvoice", target_spec)
            if target_spec else RUNTIME_ASSET)
        _version_path(data_dir).write_text(asset, encoding="utf-8")
        unpack = target / (
            target_spec.conda_unpack_relpath
            if target_spec else Path("Scripts/conda-unpack.exe"))
        if target_spec:
            prepare_runtime_tree(target, target_spec)
        if unpack.is_file():
            proc = subprocess.run(
                [str(unpack)], cwd=str(target), capture_output=True, text=True,
                timeout=600, check=False)
            if proc.returncode:
                raise RuntimeError(
                    "conda-unpack 失败：" +
                    (proc.stderr or proc.stdout or "")[-800:])
        if not runtime_files_ready(data_dir):
            raise RuntimeError("runtime 解压后文件不完整")
        shutil.rmtree(backup, ignore_errors=True)
        if cleanup_archive:
            archive.unlink(missing_ok=True)
            for part in archive.parent.glob(f"{asset}.part*"):
                part.unlink(missing_ok=True)
        _SMOKE_CACHE.clear()
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        if backup.exists():
            backup.replace(target)
        raise


def _install_runtime(data_dir: Path, *, proxy: str | None,
                     progress_cb: Progress | None,
                     cancel_event: threading.Event | None) -> None:
    if runtime_files_ready(data_dir):
        return
    archive = _download_runtime(
        data_dir, proxy=proxy, progress_cb=progress_cb,
        cancel_event=cancel_event)
    install_runtime_archive(
        data_dir, archive, progress_cb=progress_cb, cleanup_archive=True)


def download_models(data_dir: Path, *, proxy: str | None = None,
                    progress_cb: Progress | None = None,
                    cancel_event: threading.Event | None = None) -> bool:
    if models_ready(data_dir):
        return True
    env = os.environ.copy()
    env["PYTHONPATH"] = str(runtime_source(data_dir))
    env["HF_HUB_DISABLE_XET"] = "1"
    env["HF_XET_DISABLE"] = "1"
    env["HF_HUB_DOWNLOAD_TIMEOUT"] = "300"
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
    command = [
        str(runtime_python(data_dir)), str(runtime_worker(data_dir)),
        "download-model", "--model-dir", str(model_dir(data_dir)),
        "--repo", MODEL_REPO,
    ]
    for attempt in range(1, 9):
        if cancel_event and cancel_event.is_set():
            return False
        _emit(progress_cb, "下载 CosyVoice2-0.5B 模型"
              f"（可断点续传，第 {attempt}/8 次）…", 0.0)
        proc = subprocess.Popen(
            command, cwd=str(runtime_dir(data_dir)), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
        detail = ""
        assert proc.stdout is not None
        for line in proc.stdout:
            detail = (detail + line)[-1600:]
            if cancel_event and cancel_event.is_set():
                proc.terminate()
                proc.wait(timeout=30)
                return False
            if not line.startswith("MEDIA_AGENT_JSON:"):
                continue
            try:
                event = json.loads(line.split(":", 1)[1])
            except json.JSONDecodeError:
                continue
            if event.get("event") == "progress":
                fraction = float(event.get("fraction") or 0)
                _emit(progress_cb, str(event.get("message") or
                                       "下载 CosyVoice2 模型…"),
                      max(0.0, min(fraction, 1.0)))
        return_code = proc.wait()
        if return_code == 0 and models_ready(data_dir):
            _emit(progress_cb, "CosyVoice2 模型下载完成", 1.0)
            return True
        if attempt < 8:
            _emit(progress_cb, f"模型下载中断，自动续传：{detail[-400:]}",
                  0.0)
            time.sleep(min(attempt, 5))
    return False


def run_setup(data_dir: Path, *, proxy: str | None = None,
              progress_cb: Progress | None = None,
              cancel_event: threading.Event | None = None) -> dict[str, Any]:
    current = setup_status(data_dir)
    if not current["installable"]:
        return {"ok": False, "message": current["reason"], **current}
    try:
        _install_runtime(
            data_dir, proxy=proxy,
            progress_cb=(lambda msg, frac: _emit(
                progress_cb, msg, min(max(frac, 0) * .5, .5))),
            cancel_event=cancel_event)
    except Exception as exc:
        return {"ok": False, "message": str(exc), **setup_status(data_dir)}
    model_fraction = 0.0

    def model_progress(message: str, fraction: float) -> None:
        nonlocal model_fraction
        if fraction < 0:
            _emit(progress_cb, message, fraction)
            return
        model_fraction = max(model_fraction, min(fraction, 1.0))
        _emit(progress_cb, message, .5 + model_fraction * .4)

    if not download_models(
            data_dir, proxy=proxy, progress_cb=model_progress,
            cancel_event=cancel_event):
        return {"ok": False, "message": "CosyVoice2 模型下载失败",
                **setup_status(data_dir)}
    accelerator = current["accelerator"] or "runtime"
    _emit(progress_cb, f"加载模型并验证 {accelerator}（首次可能需要数分钟）…", .95)
    _SMOKE_CACHE.clear()
    ok, reason = runtime_smoke(data_dir, deep=True)
    if not ok:
        return {"ok": False, "message": f"runtime 验证失败：{reason}",
                **setup_status(data_dir)}
    status = setup_status(data_dir)
    _emit(progress_cb, "CosyVoice runtime 与模型安装完成！", 1.0)
    return {**status, "ok": status["ready"],
            "message": "CosyVoice 声音复刻已就绪" if status["ready"]
            else status["reason"]}
