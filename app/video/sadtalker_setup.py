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
    runtime_asset)

logger = logging.getLogger(__name__)

RUNTIME_TAG = "sadtalker-runtime-v1"
RUNTIME_ASSET = "sadtalker-runtime-win64-cuda118.tar.gz"
RUNTIME_MANIFEST = "sadtalker-runtime-win64-cuda118.manifest.json"
RUNTIME_RELEASE_BASE = (
    f"https://github.com/ivanzwb/release/releases/download/{RUNTIME_TAG}"
)


def runtime_target(
    system: str | None = None, machine: str | None = None,
) -> RuntimePlatform | None:
    return detect_runtime_platform(system, machine)


def _runtime_asset(target: RuntimePlatform | None = None) -> str:
    target = target or runtime_target()
    return runtime_asset("sadtalker", target) if target else RUNTIME_ASSET


def _runtime_manifest(target: RuntimePlatform | None = None) -> str:
    return _runtime_asset(target).removesuffix(".tar.gz") + ".manifest.json"

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

REQUIRED_HASHES = {
    "checkpoints/mapping_00109-model.pth.tar":
        "84a8642468a3fcfdd9ab6be955267043116c2bec2284686a5262f1eaf017f64c",
    "checkpoints/mapping_00229-model.pth.tar":
        "62a1e06006cc963220f6477438518ed86e9788226c62ae382ddc42fbcefb83f1",
    "checkpoints/SadTalker_V0.0.2_256.safetensors":
        "c211f5d6de003516bf1bbda9f47049a4c9c99133b1ab565c6961e5af16477bff",
}
_MODEL_HASH_CACHE: dict[tuple[str, int, int, str], bool] = {}

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
    target = runtime_target()
    relative = target.python_relpath if target else Path("python.exe")
    return runtime_dir(data_dir) / relative


def runtime_source(data_dir: Path) -> Path:
    return runtime_dir(data_dir) / "sadtalker-src"


def _runtime_version_path(data_dir: Path) -> Path:
    return runtime_dir(data_dir) / ".runtime-version"


def checkpoint_dir(data_dir: Path) -> Path:
    return _models_dir(data_dir) / "checkpoints"


def _runtime_archive(data_dir: Path) -> Path:
    return data_dir / "downloads" / _runtime_asset()


def _runtime_url() -> str:
    return os.environ.get(
        "MEDIA_AGENT_SADTALKER_RUNTIME_MANIFEST_URL",
        f"{RUNTIME_RELEASE_BASE}/{_runtime_manifest()}",
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
    """True if every required model file has its exact size and checksum.

    A non-empty check is unsafe for resumable downloads: a truncated checkpoint
    (or even a tiny HTTP error body from the old, invalid URL) was previously
    treated as fully installed.
    """
    d = _models_dir(data_dir)
    return all(_model_file_valid(d / rel, rel, expected)
               for rel, expected in REQUIRED_FILES)


def _model_file_valid(path: Path, rel: str, expected: int | None) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False
    if expected is not None and stat.st_size != expected:
        return False
    if expected is None and stat.st_size <= 0:
        return False
    expected_hash = REQUIRED_HASHES.get(rel)
    if not expected_hash:
        return True
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns, expected_hash)
    cached = _MODEL_HASH_CACHE.get(key)
    if cached is not None:
        return cached
    valid = _sha256(path).lower() == expected_hash.lower()
    _MODEL_HASH_CACHE[key] = valid
    return valid


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


def accelerator_status(
    target: RuntimePlatform | None = None,
) -> dict[str, Any]:
    target = target or runtime_target()
    if target is None:
        return {
            "ok": False, "name": "", "accelerator": "",
            "reason": (
                f"当前平台没有可用的 managed runtime"
                f"（{platform.system()} {platform.machine()}）"),
        }
    if target.requires_gpu:
        status = gpu_status()
        return {**status, "accelerator": target.accelerator}
    return {
        "ok": True,
        "name": f"{platform.machine()} CPU",
        "accelerator": "CPU",
        "reason": "",
    }


def runtime_files_ready(data_dir: Path) -> bool:
    py = runtime_python(data_dir)
    src = runtime_source(data_dir)
    try:
        version_ok = (
            _runtime_version_path(data_dir).read_text(encoding="utf-8").strip()
            == _runtime_asset())
    except OSError:
        version_ok = False
    return py.is_file() and (src / "inference.py").is_file() and version_ok


_SMOKE_CACHE: dict[tuple[str, int, int, bool, bool], tuple[bool, str]] = {}
_SMOKE_SCHEMA = 2
_GPU_SMOKE_TIMEOUT = 300
_CPU_SMOKE_TIMEOUT = 1800
# After all known stages appear in output, give the process this many
# seconds to finish video encoding before killing it.  SadTalker can
# hang indefinitely on GPU cleanup / ffmpeg after inference completes.
_POST_STAGES_GRACE = 120
_SMOKE_AUDIO_SECONDS = 1.0


def _clear_smoke_cache() -> None:
    _SMOKE_CACHE.clear()


def _smoke_marker(data_dir: Path) -> Path:
    return runtime_dir(data_dir) / ".inference-smoke-ok.json"


def _smoke_failure_marker(data_dir: Path) -> Path:
    return runtime_dir(data_dir) / ".inference-smoke-failure.json"


def _smoke_fingerprint(data_dir: Path) -> dict[str, Any]:
    py = runtime_python(data_dir)
    inference = runtime_source(data_dir) / "inference.py"

    def file_identity(path: Path) -> dict[str, int]:
        try:
            stat = path.stat()
            return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        except OSError:
            return {"size": 0, "mtime_ns": 0}

    return {
        "schema": _SMOKE_SCHEMA,
        "runtime": _runtime_asset(),
        "python": file_identity(py),
        "inference": file_identity(inference),
        "models": {
            rel: file_identity(checkpoint_dir(data_dir) / Path(rel).name)
            for rel, _ in REQUIRED_FILES
        },
    }


def _deep_smoke_ready(data_dir: Path) -> bool:
    try:
        saved = json.loads(_smoke_marker(data_dir).read_text(encoding="utf-8"))
        return saved == _smoke_fingerprint(data_dir)
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _last_smoke_failure(data_dir: Path) -> str:
    """Return the last deep-inference failure for the current installed assets."""
    try:
        saved = json.loads(
            _smoke_failure_marker(data_dir).read_text(encoding="utf-8"))
        if saved.get("fingerprint") == _smoke_fingerprint(data_dir):
            return str(saved.get("reason") or "").strip()
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return ""


def _subprocess_reason(
    process: subprocess.CompletedProcess[str], fallback: str,
) -> str:
    """Preserve both output streams; stderr alone often omits the real cause."""
    sections: list[str] = []
    stdout = (process.stdout or "").strip()
    stderr = (process.stderr or "").strip()
    if stdout:
        sections.append(f"stdout:\n{stdout}")
    if stderr:
        sections.append(f"stderr:\n{stderr}")
    return "\n\n".join(sections) or fallback


def _emit_smoke_progress(
    callback: Callable[[str, float], None] | None,
    message: str,
    fraction: float,
) -> None:
    if callback:
        try:
            callback(message, fraction)
        except Exception:
            pass


def _prepare_smoke_assets(source: Path, work_dir: Path) -> tuple[Path, Path]:
    """Create deterministic one-second inputs without invoking ffmpeg."""
    import wave

    from PIL import Image

    example_face = source / "examples" / "source_image" / "happy.png"
    example_audio = source / "examples" / "driven_audio" / "bus_chinese.wav"
    if not example_face.is_file() or not example_audio.is_file():
        raise FileNotFoundError("runtime 缺少 SadTalker 推理验证素材")
    work_dir.mkdir(parents=True, exist_ok=True)
    face = work_dir / "face.png"
    audio = work_dir / "audio-1s.wav"
    with Image.open(example_face) as image:
        image.convert("RGB").resize((256, 256)).save(face)
    with wave.open(str(example_audio), "rb") as source_wav:
        params = source_wav.getparams()
        frames = source_wav.readframes(
            min(params.nframes, int(params.framerate * _SMOKE_AUDIO_SECONDS)))
    with wave.open(str(audio), "wb") as output_wav:
        output_wav.setparams(params)
        output_wav.writeframes(frames)
    return face, audio


_SMOKE_RUNNER = (
    "import runpy,sys,torch\n"
    "script=sys.argv[1]\n"
    "expected=sys.argv[2]\n"
    "actual='cuda' if torch.cuda.is_available() else 'cpu'\n"
    "print('[media-agent] selected_device='+actual, flush=True)\n"
    "if actual != expected:\n"
    " raise RuntimeError('SadTalker device mismatch: expected '+expected"
    "+', got '+actual)\n"
    "sys.argv=[script]+sys.argv[3:]\n"
    "runpy.run_path(script, run_name='__main__')\n"
)


def _smoke_command(
    python: Path,
    source: Path,
    checkpoints: Path,
    result_dir: Path,
    face: Path,
    audio: Path,
    *,
    device: str,
) -> list[str]:
    """Build the validation-only SadTalker command."""
    command = [
        str(python), "-c", _SMOKE_RUNNER,
        str(source / "inference.py"), device,
        "--driven_audio", str(audio),
        "--source_image", str(face),
        "--checkpoint_dir", str(checkpoints),
        "--result_dir", str(result_dir),
        "--size", "256",
        "--batch_size", "2",
        "--preprocess", "resize",
        "--still",
    ]
    if device == "cpu":
        command.append("--cpu")
    return command


def _output_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _inference_stage(output: str) -> tuple[str, float]:
    if "The generated video is named" in output:
        return "编码并封装验证视频", 0.9
    if "mel:" in output or "audio" in output.lower():
        return "运行音频驱动与短帧生成", 0.7
    if "3DMM Extraction" in output:
        return "提取单帧人脸参数", 0.5
    if "[media-agent] selected_device=" in output:
        return "加载 SadTalker 检查点", 0.3
    return "启动 SadTalker runtime", 0.2


def _all_stages_seen(output: str) -> bool:
    """Return True when all visible inference stages have appeared in output."""
    _, fraction = _inference_stage(output)
    return fraction >= 0.7


def _run_smoke_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    progress_cb: Callable[[str, float], None] | None,
) -> tuple[subprocess.CompletedProcess[str], float, bool]:
    """Run inference with heartbeat progress and retain complete output."""
    started = time.monotonic()
    creationflags = (
        (getattr(subprocess, "CREATE_NO_WINDOW", 0)
         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        if os.name == "nt" else 0)
    process = subprocess.Popen(
        command, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", creationflags=creationflags,
        start_new_session=(os.name != "nt"))
    partial_stdout = ""
    partial_stderr = ""
    timed_out = False
    stages_seen_at: float | None = None  # when all inference stages first seen

    def _kill() -> None:
        if os.name == "nt" and getattr(process, "pid", None):
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, text=True, timeout=15, check=False)
        elif getattr(process, "pid", None):
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
        else:
            process.kill()

    while True:
        elapsed = time.monotonic() - started
        remaining = timeout - elapsed
        if remaining <= 0:
            timed_out = True
            _kill()
            stdout, stderr = process.communicate()
            break
        try:
            stdout, stderr = process.communicate(timeout=min(8, remaining))
            break
        except subprocess.TimeoutExpired as exc:
            partial_stdout = _output_text(exc.stdout or exc.output)
            partial_stderr = _output_text(exc.stderr)
            combined = partial_stdout + "\n" + partial_stderr
            elapsed = time.monotonic() - started
            stage, fraction = _inference_stage(combined)
            _emit_smoke_progress(
                progress_cb,
                f"{stage}（已用时 {int(elapsed)} 秒）…",
                fraction,
            )
            # Grace-period exit: if all visible stages have completed,
            # the process is likely stuck in video encoding / GPU cleanup.
            # Kill it after _POST_STAGES_GRACE seconds — inference itself
            # succeeded, so treat this as NOT timed out.
            if _all_stages_seen(combined):
                if stages_seen_at is None:
                    stages_seen_at = time.monotonic()
                elif (time.monotonic() - stages_seen_at) > _POST_STAGES_GRACE:
                    _emit_smoke_progress(
                        progress_cb,
                        f"推理已完成，编码进程超时（{int(elapsed)} 秒），终止中…",
                        0.9,
                    )
                    _kill()
                    stdout, stderr = process.communicate()
                    break
            else:
                stages_seen_at = None  # reset if new output reverses
    elapsed = time.monotonic() - started
    stdout = _output_text(stdout) or partial_stdout
    stderr = _output_text(stderr) or partial_stderr
    completed = subprocess.CompletedProcess(
        command, process.returncode, stdout, stderr)
    return completed, elapsed, timed_out


def _inspect_smoke_video(
    python: Path, video: Path, *, cwd: Path, env: dict[str, str],
) -> tuple[bool, str]:
    code = (
        "import cv2,json,sys\n"
        "cap=cv2.VideoCapture(sys.argv[1])\n"
        "ok,frame=cap.read()\n"
        "info={'opened':cap.isOpened(),'decoded':bool(ok),"
        "'frames':int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),"
        "'fps':float(cap.get(cv2.CAP_PROP_FPS)),"
        "'width':int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),"
        "'height':int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),"
        "'std':float(frame.std()) if ok else 0.0}\n"
        "print(json.dumps(info))\n"
    )
    check = subprocess.run(
        [str(python), "-c", code, str(video)], cwd=str(cwd), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, check=False)
    if check.returncode != 0:
        return False, _subprocess_reason(check, "无法读取验证视频")
    try:
        info = json.loads((check.stdout or "").strip().splitlines()[-1])
    except (IndexError, ValueError, json.JSONDecodeError):
        return False, _subprocess_reason(check, "验证视频元数据无效")
    valid = bool(
        info.get("opened") and info.get("decoded")
        and 12 <= int(info.get("frames", 0)) <= 40
        and int(info.get("width", 0)) == 256
        and int(info.get("height", 0)) == 256
        and 20 <= float(info.get("fps", 0)) <= 30
        and float(info.get("std", 0)) > 1.0
    )
    return valid, "" if valid else f"验证视频内容无效：{info}"


def runtime_smoke(
    data_dir: Path, *, require_gpu: bool | None = None, deep: bool = False,
    progress_cb: Callable[[str, float], None] | None = None,
) -> tuple[bool, str]:
    """Verify imports/accelerator and optionally complete one real inference."""
    data_dir = Path(data_dir).resolve()
    if not runtime_files_ready(data_dir):
        return False, "SadTalker runtime 文件不完整"
    if deep and _deep_smoke_ready(data_dir):
        _emit_smoke_progress(progress_cb, "已使用通过验证的 runtime 缓存", 1.0)
        return True, ""
    py = runtime_python(data_dir)
    source = runtime_source(data_dir)
    target = runtime_target()
    if target is None:
        return False, "当前平台不支持 SadTalker managed runtime"
    if require_gpu is None:
        require_gpu = target.requires_gpu
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
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    try:
        _emit_smoke_progress(progress_cb, "检查 runtime 与加速设备…", 0.05)
        # Deep validation's wrapper performs the same device assertion before
        # importing inference.py. Avoid two extra interpreter/import passes:
        # torch + torchvision startup can itself take tens of seconds.
        check = (
            subprocess.CompletedProcess([], 0, "", "")
            if deep else subprocess.run(
                [str(py), "-c", code],
                cwd=str(source), env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=90, check=False,
            )
        )
        if check.returncode != 0:
            result = (False, _subprocess_reason(check, "runtime import failed"))
        else:
            help_check = (
                subprocess.CompletedProcess([], 0, "", "")
                if deep else subprocess.run(
                    [str(py), str(source / "inference.py"), "--help"],
                    cwd=str(source), env=env, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=90,
                    check=False,
                )
            )
            if help_check.returncode != 0:
                result = (False, _subprocess_reason(
                    help_check, "SadTalker import failed"))
            elif deep:
                result_dir = runtime_dir(data_dir) / ".smoke-result"
                shutil.rmtree(result_dir, ignore_errors=True)
                face, audio = _prepare_smoke_assets(
                    source, result_dir / "input")
                _emit_smoke_progress(
                    progress_cb, "已准备 1 秒、256×256 验证素材", 0.1)
                command = _smoke_command(
                    py, source, checkpoint_dir(data_dir), result_dir,
                    face, audio, device=target.device)
                timeout = (
                    _CPU_SMOKE_TIMEOUT if target.slow
                    else _GPU_SMOKE_TIMEOUT)
                infer, elapsed, timed_out = _run_smoke_process(
                    command, cwd=source, env=env, timeout=timeout,
                    progress_cb=progress_cb)
                videos = list(result_dir.glob("*.mp4"))
                selected = f"[media-agent] selected_device={target.device}"
                if timed_out:
                    result = (
                        False,
                        f"SadTalker 验证超过 {timeout} 秒，已终止"
                        f"（实际用时 {elapsed:.1f} 秒）\n"
                        + _subprocess_reason(infer, "无子进程输出"))
                elif infer.returncode != 0:
                    result = (
                        False,
                        f"SadTalker 验证失败（用时 {elapsed:.1f} 秒）\n"
                        + _subprocess_reason(infer, "推理进程失败"))
                elif selected not in (infer.stdout or ""):
                    result = (
                        False,
                        "SadTalker 未确认使用预期设备，拒绝标记为已就绪\n"
                        + _subprocess_reason(infer, "缺少设备确认输出"))
                elif not videos:
                    result = (
                        False,
                        f"SadTalker 未生成验证视频（用时 {elapsed:.1f} 秒）\n"
                        + _subprocess_reason(infer, "无子进程输出"))
                else:
                    _emit_smoke_progress(
                        progress_cb, "检查验证视频帧与内容…", 0.95)
                    content_ok, content_reason = _inspect_smoke_video(
                        py, videos[0], cwd=source, env=env)
                    if content_ok:
                        _smoke_marker(data_dir).write_text(
                            json.dumps(_smoke_fingerprint(data_dir), indent=2),
                            encoding="utf-8")
                        _smoke_failure_marker(data_dir).unlink(missing_ok=True)
                        _emit_smoke_progress(
                            progress_cb,
                            f"实际推理验证通过（用时 {elapsed:.1f} 秒）",
                            1.0)
                        result = (True, "")
                    else:
                        result = (False, content_reason)
                shutil.rmtree(result_dir, ignore_errors=True)
            else:
                result = (True, "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = (False, str(exc))
    if deep and not result[0]:
        _smoke_failure_marker(data_dir).parent.mkdir(
            parents=True, exist_ok=True)
        _smoke_failure_marker(data_dir).write_text(json.dumps({
            "fingerprint": _smoke_fingerprint(data_dir),
            "reason": result[1],
            "failed_at": int(time.time()),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    _SMOKE_CACHE[key] = result
    return result


def setup_status(data_dir: Path) -> dict[str, Any]:
    """Return complete readiness; model files alone are not installation."""
    d = _models_dir(data_dir)
    models_ok = models_ready(data_dir)
    runtime_ok = runtime_files_ready(data_dir)
    target = runtime_target()
    device = accelerator_status(target)
    smoke_ok = False
    smoke_reason = ""
    if target is None:
        smoke_reason = device["reason"]
    elif not device["ok"]:
        smoke_reason = device["reason"]
    elif runtime_ok and models_ok and _deep_smoke_ready(data_dir):
        smoke_ok = True
    elif runtime_ok and models_ok:
        smoke_reason = (
            _last_smoke_failure(data_dir)
            or "尚未完成 SadTalker 实际推理验证")
    elif not runtime_ok:
        smoke_reason = "尚未安装 SadTalker runtime"
    else:
        smoke_reason = "SadTalker 模型文件不完整"
    ready = bool(models_ok and runtime_ok and device["ok"] and smoke_ok)
    installed = bool(models_ok and runtime_ok)
    if ready:
        state = "ready"
    elif target is None:
        state = "unsupported"
    elif not device["ok"]:
        state = "device_unavailable"
    elif installed and _last_smoke_failure(data_dir):
        state = "validation_failed"
    elif installed:
        state = "validation_pending"
    elif runtime_ok or models_ok:
        state = "partially_installed"
    else:
        state = "not_installed"
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
            "ok": (
                _model_file_valid(fp, f, expected)
                if (f, expected) in REQUIRED_FILES
                else exists and (expected is None or size > 0)
            ),
            "sha256": REQUIRED_HASHES.get(f),
        })
    return {
        "ready": ready,
        "installed": installed,
        "state": state,
        "retry_validation": bool(installed and device["ok"] and not smoke_ok),
        "supported": target is not None,
        "asset_available": target is not None,
        "installable": bool(target is not None and device["ok"]),
        "platform": target.key if target else "unsupported",
        "accelerator": target.accelerator if target else "",
        "device_ok": device["ok"],
        "device_name": device["name"],
        "performance_warning": (
            "macOS 使用 CPU 推理，生成速度可能较慢"
            if target is not None and target.slow else ""),
        "gpu_ok": bool(device["ok"] and target is not None
                       and target.requires_gpu),
        "gpu_name": device["name"] if target and target.requires_gpu else "",
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


def _model_urls(path: str, mirror: bool = True) -> list[str]:
    official = _model_url(path, mirror=False)
    urls: list[str] = []
    configured = os.environ.get("MEDIA_AGENT_SADTALKER_MODEL_MIRROR", "").strip()
    if configured:
        urls.append(configured.format(
            filename=Path(path).name, url=official, path=path))
    elif mirror:
        # Maintained by SadTalker's author and substantially faster in China.
        urls.append(
            "https://hf-mirror.com/vinthony/SadTalker-V002rc/"
            f"resolve/main/{Path(path).name}"
        )
    urls.append(official)
    if mirror:
        gh_mirror = os.environ.get("MEDIA_AGENT_GITHUB_MIRROR", "").strip()
        if gh_mirror:
            urls.append(f"{gh_mirror.rstrip('/')}/{official}")
    return list(dict.fromkeys(urls))


def _download_models_with_curl(
    curl: str,
    data_dir: Path,
    *,
    mirror: bool,
    proxy: str | None,
    progress_cb: Callable[[str, float], None] | None,
    cancel_event: threading.Event | None,
    pause_event: threading.Event | None = None,
) -> bool:
    dest = _models_dir(data_dir)
    dest.mkdir(parents=True, exist_ok=True)
    prog = _load_progress(data_dir)
    total_bytes = sum(size for _, size in REQUIRED_FILES if size)
    completed = 0

    def emit(message: str, fraction: float) -> None:
        if progress_cb:
            try:
                progress_cb(message, fraction)
            except Exception:
                pass

    def wait_if_paused() -> None:
        if pause_event and pause_event.is_set():
            emit("下载已暂停 — 可修改镜像后继续",
                 completed / max(total_bytes, 1))
            while pause_event.is_set():
                if cancel_event and cancel_event.is_set():
                    emit("下载已取消", -1)
                    raise RuntimeError("安装已取消")
                time.sleep(0.5)

    emit("使用 curl 下载 SadTalker 模型（支持断点续传）…", 0.0)
    for rel, expected in REQUIRED_FILES:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        current = target.stat().st_size if target.is_file() else 0
        if _model_file_valid(target, rel, expected):
            completed += expected
            continue
        if expected and current == expected:
            emit(f"{Path(rel).name} 校验失败，重新下载…",
                 completed / max(total_bytes, 1))
            target.unlink()
            current = 0
        if expected and current > expected:
            target.unlink()
            current = 0

        urls = _model_urls(rel, mirror=mirror)
        official_url = _model_url(rel, mirror=False)
        success = False
        for attempt in range(1, 9):
            if cancel_event and cancel_event.is_set():
                emit("下载已取消", -1)
                return False
            wait_if_paused()
            current = target.stat().st_size if target.is_file() else 0
            url = urls[(attempt - 1) % len(urls)]
            route = "官方源" if url == official_url else "备用镜像"
            emit(
                f"{route}下载 {Path(rel).name}"
                f"（第 {attempt}/8 次，从 {current / 1048576:.1f} MB 续传）…",
                completed / max(total_bytes, 1),
            )
            command = [
                curl, "--location", "--fail", "--silent", "--show-error",
                "--connect-timeout", "15", "--speed-limit", "1024",
                "--speed-time", "60", "--continue-at", "-",
                "--output", str(target), url,
            ]
            if proxy:
                command[1:1] = ["--proxy", proxy]
            creationflags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if os.name == "nt" else 0
            )
            process = subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, creationflags=creationflags)
            previous_size = current
            previous_at = time.monotonic()
            while process.poll() is None:
                if cancel_event and cancel_event.is_set():
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    emit("下载已取消", -1)
                    return False
                if pause_event and pause_event.is_set():
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    emit("下载已暂停 — 可修改镜像后继续",
                         completed / max(total_bytes, 1))
                    while pause_event.is_set():
                        if cancel_event and cancel_event.is_set():
                            emit("下载已取消", -1)
                            return False
                        time.sleep(0.5)
                    break  # break inner while to restart attempt loop
                time.sleep(1)
                size = target.stat().st_size if target.is_file() else 0
                now = time.monotonic()
                speed = (size - previous_size) / max(now - previous_at, 0.001)
                emit(
                    f"下载 {Path(rel).name}："
                    f"{size / 1048576:.1f}/{expected / 1048576:.1f} MB · "
                    f"{max(speed, 0) / 1048576:.2f} MB/s",
                    min((completed + size) / max(total_bytes, 1), 0.99),
                )
                previous_size = size
                previous_at = now

            _stdout, stderr = process.communicate()
            current = target.stat().st_size if target.is_file() else 0
            prog[rel] = current
            _save_progress(data_dir, prog)
            if (process.returncode == 0
                    and _model_file_valid(target, rel, expected)):
                emit(
                    f"{Path(rel).name} 下载完成"
                    f"（{current / 1048576:.1f} MB）",
                    min((completed + current) / max(total_bytes, 1), 0.99),
                )
                success = True
                break

            reason = (stderr or "").strip().splitlines()
            reason_text = reason[-1] if reason else (
                f"curl 退出码 {process.returncode}，文件大小 "
                f"{current}/{expected}")
            if process.returncode == 0 and current == expected:
                reason_text = "SHA-256 校验失败"
                target.unlink(missing_ok=True)
                current = 0
            logger.warning(
                "Model download failed for %s via %s (attempt %d/8): %s",
                rel, route, attempt, reason_text)
            if attempt < 8:
                emit(
                    f"{route}失败：{reason_text}；"
                    f"将从 {current / 1048576:.1f} MB 自动续传…",
                    completed / max(total_bytes, 1),
                )
                time.sleep(min(attempt, 5))
            else:
                emit(f"下载 {rel} 失败：{reason_text}", -1)
        if not success:
            return False
        completed += expected

    _progress_path(data_dir).unlink(missing_ok=True)
    emit("数字人模型安装完成！", 1.0)
    return models_ready(data_dir)


def download_models(
    data_dir: Path,
    *,
    mirror: bool = True,
    proxy: str | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
    cancel_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
) -> bool:
    """Download *all* model files with per-file resume.

    Parameters
    ----------
    progress_cb:
        Called with ``(message, fraction)`` where *fraction* is 0.0-1.0 or
        -1 on error.  Intended for SSE streaming to the frontend.
    cancel_event:
        If set, the download loop exits early.
    pause_event:
        If set, download pauses and waits until cleared.

    Returns True when all *required* files are present.
    """
    curl = shutil.which("curl")
    if curl:
        return _download_models_with_curl(
            curl, data_dir, mirror=mirror, proxy=proxy,
            progress_cb=progress_cb, cancel_event=cancel_event,
            pause_event=pause_event)

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
                elif _model_file_valid(fp, rel, expected):
                    downloaded_so_far += expected
                    continue
                elif expected and fp.stat().st_size == expected:
                    _emit(f"{Path(rel).name} 校验失败，重新下载…",
                          downloaded_so_far / max(total_bytes, 1))
                    fp.unlink()
                    existing = 0
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
                if pause_event and pause_event.is_set():
                    _emit("下载已暂停 — 可修改镜像后继续",
                          downloaded_so_far / max(total_bytes, 1))
                    while pause_event.is_set():
                        if cancel_event and cancel_event.is_set():
                            _emit("下载已取消", -1)
                            return False
                        time.sleep(0.5)
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
                                if pause_event and pause_event.is_set():
                                    _emit("下载已暂停 — 可修改镜像后继续",
                                          (downloaded_so_far + bytes_written) / max(total_bytes, 1))
                                    while pause_event.is_set():
                                        if cancel_event and cancel_event.is_set():
                                            _emit("下载已取消", -1)
                                            return False
                                        time.sleep(0.5)
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
    pause_event: threading.Event | None = None,
) -> None:
    """Download one release-asset part, resuming an interrupted local file."""
    for attempt in range(1, 9):
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("安装已取消")
        if pause_event and pause_event.is_set():
            emit("下载已暂停 — 可修改镜像后继续",
                 completed_bytes / max(total_bytes, 1))
            while pause_event.is_set():
                if cancel_event and cancel_event.is_set():
                    raise RuntimeError("安装已取消")
                time.sleep(0.5)
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
                        if pause_event and pause_event.is_set():
                            emit("下载已暂停 — 可修改镜像后继续",
                                 (completed_bytes + written) / max(total_bytes, 1))
                            while pause_event.is_set():
                                if cancel_event and cancel_event.is_set():
                                    raise RuntimeError("安装已取消")
                                time.sleep(0.5)
                        output.write(chunk)
                        written += len(chunk)
                        now = time.monotonic()
                        if now - last_at >= 1:
                            speed = (written - last_bytes) / max(
                                now - last_at, 0.001)
                            overall = (completed_bytes + written) / max(
                                total_bytes, 1)
                            emit(
                                "下载 SadTalker runtime："
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
    pause_event: threading.Event | None = None,
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
                cancel_event=cancel_event, pause_event=pause_event,
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
            if member.issym() or member.islnk():
                link = Path(member.name).parent / member.linkname
                try:
                    (target / link).resolve().relative_to(target_abs)
                except ValueError as exc:
                    raise RuntimeError(
                        f"runtime 压缩包包含不安全链接：{member.name}") from exc
        bundle.extractall(target)


def install_runtime_archive(
    data_dir: Path,
    archive: Path,
    *,
    progress_cb: Callable[[str, float], None] | None = None,
    cleanup_archive: bool = False,
) -> None:
    """Install a locally built archive into the same managed release layout."""
    archive = Path(archive).resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"SadTalker runtime archive 不存在：{archive}")
    target = runtime_dir(data_dir)
    staging = target.with_name(target.name + ".installing")
    backup = target.with_name(target.name + ".previous")
    if _runtime_payload_ready(target):
        if progress_cb:
            progress_cb("检测到已解压 runtime，继续完成激活…", 0.98)
        _finish_runtime_install(data_dir, target, progress_cb)
        return

    if not _runtime_payload_ready(staging):
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        if progress_cb:
            progress_cb("解压 SadTalker runtime（可能需要数分钟）…", 0.90)
        _safe_extract(archive, staging)
    if not _runtime_payload_ready(staging):
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError("runtime 压缩包无效：缺少 Python 或 inference.py")
    if progress_cb:
        progress_cb("runtime 解压完成，切换到正式目录…", 0.97)

    shutil.rmtree(backup, ignore_errors=True)
    moved_target = False
    try:
        if target.exists():
            _replace_with_retry(target, backup)
            moved_target = True
        _replace_with_retry(staging, target)
        _finish_runtime_install(data_dir, target, progress_cb)
        shutil.rmtree(backup, ignore_errors=True)
        if cleanup_archive:
            archive.unlink(missing_ok=True)
            for part in archive.parent.glob(f"{_runtime_asset()}.part*"):
                part.unlink(missing_ok=True)
        _clear_smoke_cache()
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        if moved_target and backup.exists():
            _replace_with_retry(backup, target)
        raise


def _runtime_payload_ready(path: Path) -> bool:
    target = runtime_target()
    python_relative = target.python_relpath if target else Path("python.exe")
    return (
        (path / python_relative).is_file()
        and (path / "sadtalker-src" / "inference.py").is_file()
    )


def _finish_runtime_install(
    data_dir: Path,
    target: Path,
    progress_cb: Callable[[str, float], None] | None,
) -> None:
    target_spec = runtime_target()
    unpack_relative = (
        target_spec.conda_unpack_relpath if target_spec
        else Path("Scripts/conda-unpack.exe"))
    unpack = target / unpack_relative
    if target_spec:
        prepare_runtime_tree(target, target_spec)
    if unpack.is_file():
        if progress_cb:
            progress_cb("激活 SadTalker runtime（可能需要数分钟）…", 0.98)
        proc = subprocess.run(
            [str(unpack)], cwd=str(target), capture_output=True, text=True,
            timeout=900, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                "conda-unpack 失败：" +
                (proc.stderr or proc.stdout or "")[-800:])
    _runtime_version_path(data_dir).write_text(
        _runtime_asset(), encoding="utf-8")
    if not runtime_files_ready(data_dir):
        _runtime_version_path(data_dir).unlink(missing_ok=True)
        raise RuntimeError("runtime 激活后文件不完整")
    if progress_cb:
        progress_cb("SadTalker runtime 已激活", 1.0)
    _clear_smoke_cache()


def _replace_with_retry(source: Path, target: Path) -> None:
    """Rename a runtime directory despite short-lived Windows AV locks."""
    last_error: OSError | None = None
    for attempt in range(8):
        try:
            source.replace(target)
            return
        except OSError as exc:
            last_error = exc
            if attempt == 7:
                break
            time.sleep(0.25 * (2 ** attempt))
    assert last_error is not None
    raise last_error


def _install_runtime(
    data_dir: Path,
    *,
    proxy: str | None,
    progress_cb: Callable[[str, float], None] | None,
    cancel_event: threading.Event | None,
    pause_event: threading.Event | None = None,
) -> None:
    if runtime_files_ready(data_dir):
        return
    archive = _download_runtime(
        data_dir, proxy=proxy, progress_cb=progress_cb,
        cancel_event=cancel_event, pause_event=pause_event)
    install_runtime_archive(
        data_dir, archive, progress_cb=progress_cb, cleanup_archive=True)


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
    pause_event: threading.Event | None = None,
    validation_only: bool = False,
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

    target = runtime_target()
    device = accelerator_status(target)
    _emit(
        f"检查 {target.accelerator if target else 'runtime'} 运行环境…", 0.0)
    if target is None or not device["ok"]:
        return {
            "ok": False, "message": device["reason"], **setup_status(data_dir)}

    if validation_only:
        current = setup_status(data_dir)
        if not current["runtime_ok"] or not current["models_ok"]:
            return {
                **current,
                "ok": False,
                "message": "Runtime 或模型尚未完整安装，无法仅重试推理验证",
            }
    else:
        # Runtime is deliberately separate from the ~1 GB model payload so
        # either can be resumed/repaired without re-downloading the other.
        try:
            _install_runtime(
                data_dir, proxy=proxy,
                progress_cb=(
                    (lambda message, fraction:
                     _emit(message, min(max(fraction, 0.0) * 0.45, 0.45)))
                    if progress_cb else None
                ),
                cancel_event=cancel_event, pause_event=pause_event,
            )
        except Exception as exc:
            status = setup_status(data_dir)
            return {**status, "ok": False, "message": str(exc)}

        def model_progress(message: str, fraction: float) -> None:
            _emit(message, fraction if fraction < 0 else 0.45 + fraction * 0.5)

        ok = download_models(
            data_dir, mirror=mirror, proxy=proxy,
            progress_cb=model_progress, cancel_event=cancel_event,
            pause_event=pause_event)
        if not ok:
            status = setup_status(data_dir)
            return {
                **status, "ok": False,
                "message": "模型下载失败，请检查网络后重试",
            }

    _emit(
        "运行 SadTalker 实际推理验证"
        + ("（CPU 首次运行可能需要较长时间）…" if target.slow
           else "（首次安装可能需要数分钟）…"),
        0.97)
    _clear_smoke_cache()

    def smoke_progress(message: str, fraction: float) -> None:
        _emit(message, 0.97 + min(max(fraction, 0.0), 1.0) * 0.029)

    smoke_ok, reason = runtime_smoke(
        data_dir, deep=True, progress_cb=smoke_progress)
    if not smoke_ok:
        status = setup_status(data_dir)
        return {
            **status,
            "ok": False,
            "message": f"runtime 验证失败：\n{reason}",
        }

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
