from __future__ import annotations

import hashlib
import io
import json
import runpy
import tarfile
from pathlib import Path

from app import cosyvoice_install as cv


def _model_files(data_dir: Path) -> None:
    root = cv.model_dir(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in cv.MODEL_REQUIRED:
        path = root / name
        path.write_bytes(("model-" + name).encode())
        files[name] = path.stat().st_size
    (root / ".model-ready.json").write_text(json.dumps({
        "repo": cv.MODEL_REPO, "files": files,
    }), encoding="utf-8")


def test_managed_layout_never_needs_user_paths(tmp_path: Path):
    assert cv.runtime_dir(tmp_path) == tmp_path / "runtimes" / "cosyvoice"
    assert cv.model_dir(tmp_path) == (
        tmp_path / "models" / "cosyvoice" / "CosyVoice2-0.5B")


def test_models_ready_validates_manifest_sizes(tmp_path: Path):
    _model_files(tmp_path)
    assert cv.models_ready(tmp_path)
    (cv.model_dir(tmp_path) / cv.MODEL_REQUIRED[0]).write_bytes(b"truncated")
    assert not cv.models_ready(tmp_path)


def test_local_archive_installs_release_layout(tmp_path: Path):
    payload = tmp_path / "payload"
    source = payload / "cosyvoice-src" / "cosyvoice" / "cli"
    source.mkdir(parents=True)
    (source / "cosyvoice.py").write_text("", encoding="utf-8")
    (payload / "python.exe").write_bytes(b"python")
    (payload / "cosyvoice-worker.py").write_text("", encoding="utf-8")
    archive = tmp_path / cv.RUNTIME_ASSET
    with tarfile.open(archive, "w:gz") as bundle:
        for path in payload.rglob("*"):
            bundle.add(path, arcname=str(path.relative_to(payload)))
    data = tmp_path / "data"
    cv.install_runtime_archive(data, archive)
    assert cv.runtime_files_ready(data)
    assert cv.runtime_python(data) == data / "runtimes" / "cosyvoice" / "python.exe"
    assert archive.exists()


def test_safe_extract_rejects_traversal(tmp_path: Path):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("../escape")
        info.size = 3
        bundle.addfile(info, io.BytesIO(b"bad"))
    try:
        cv._safe_extract(archive, tmp_path / "target")
    except RuntimeError as exc:
        assert "不安全路径" in str(exc)
    else:
        raise AssertionError("unsafe archive accepted")


def test_runtime_part_resume_uses_range(tmp_path: Path):
    destination = tmp_path / "part"
    destination.write_bytes(b"abc")
    expected = b"abcdef"

    class Response:
        status_code = 206
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def raise_for_status(self): return None
        def iter_bytes(self, _): yield b"def"

    class Client:
        headers = None
        def stream(self, _method, _url, headers):
            self.headers = headers
            return Response()

    client = Client()
    cv._download_part(
        client, url="https://example/part", destination=destination,
        expected_size=len(expected),
        expected_sha=hashlib.sha256(expected).hexdigest(),
        completed=0, total=len(expected), progress_cb=None, cancel_event=None)
    assert client.headers == {"Range": "bytes=3-"}
    assert destination.read_bytes() == expected


def test_status_requires_deep_smoke_marker(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "app.video.sadtalker_setup.gpu_status",
        lambda: {"ok": True, "name": "GPU", "reason": ""})
    payload = tmp_path / "payload"
    source = payload / "cosyvoice-src" / "cosyvoice" / "cli"
    source.mkdir(parents=True)
    (source / "cosyvoice.py").write_text("", encoding="utf-8")
    (payload / "python.exe").write_bytes(b"python")
    (payload / "cosyvoice-worker.py").write_text("", encoding="utf-8")
    archive = tmp_path / cv.RUNTIME_ASSET
    with tarfile.open(archive, "w:gz") as bundle:
        for path in payload.rglob("*"):
            bundle.add(path, arcname=str(path.relative_to(payload)))
    cv.install_runtime_archive(tmp_path, archive)
    _model_files(tmp_path)
    assert not cv.setup_status(tmp_path)["ready"]
    cv._smoke_path(tmp_path).write_text(
        json.dumps(cv._fingerprint(tmp_path)), encoding="utf-8")
    assert cv.setup_status(tmp_path)["ready"]


def test_macos_arm64_cpu_target_is_installable_without_gpu(
        tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cv.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cv.platform, "machine", lambda: "arm64")
    monkeypatch.delenv(
        "MEDIA_AGENT_COSYVOICE_RUNTIME_MANIFEST_URL", raising=False)
    monkeypatch.setattr(
        "app.video.sadtalker_setup.gpu_status",
        lambda: (_ for _ in ()).throw(AssertionError("GPU probe not expected")))

    status = cv.setup_status(tmp_path)

    assert status["platform"] == "macos-arm64-cpu"
    assert status["accelerator"] == "CPU"
    assert status["supported"] is True
    assert status["asset_available"] is True
    assert status["installable"] is True
    assert status["device_ok"] is True
    assert status["gpu_ok"] is False
    assert "CPU" in status["performance_warning"]


def test_macos_cpu_target_becomes_installable_with_manifest_override(
        tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cv.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cv.platform, "machine", lambda: "arm64")
    monkeypatch.setenv(
        "MEDIA_AGENT_COSYVOICE_RUNTIME_MANIFEST_URL",
        "https://example.invalid/cosyvoice-macos.manifest.json")

    status = cv.setup_status(tmp_path)

    assert status["asset_available"] is True
    assert status["installable"] is True
    assert status["device_ok"] is True
    assert status["gpu_ok"] is False


def test_macos_intel_cpu_target_is_installable(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cv.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cv.platform, "machine", lambda: "x86_64")

    status = cv.setup_status(tmp_path)

    assert status["platform"] == "macos-x64-cpu"
    assert status["asset_available"] is True
    assert status["installable"] is True
    assert cv.runtime_python(tmp_path) == (
        tmp_path / "runtimes" / "cosyvoice" / "bin" / "python")


def test_unsupported_platform_has_actionable_reason(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cv.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cv.platform, "machine", lambda: "x86_64")

    status = cv.setup_status(tmp_path)

    assert status["supported"] is False
    assert status["installable"] is False
    assert "Linux x86_64" in status["reason"]


def test_deep_smoke_failure_preserves_streams_and_installed_state(
        tmp_path: Path, monkeypatch):
    target = cv.runtime_target("Windows", "AMD64")
    assert target is not None
    monkeypatch.setattr(cv, "runtime_target", lambda *_a, **_k: target)
    monkeypatch.setattr(cv, "runtime_files_ready", lambda _: True)
    monkeypatch.setattr(cv, "models_ready", lambda _: True)
    monkeypatch.setattr(
        "app.video.sadtalker_setup.gpu_status",
        lambda: {"ok": True, "name": "GPU", "reason": ""})
    monkeypatch.setattr(cv, "_run_smoke_process", lambda *_a, **_k: (
        cv.subprocess.CompletedProcess(
            [], 1, "加载模型：中文诊断\n完整 stdout\n",
            "CUDA 内存不足\n完整 traceback\n"),
        12.5,
        False,
    ))

    ok, reason = cv.runtime_smoke(tmp_path, deep=True)

    assert ok is False
    assert "加载模型：中文诊断\n完整 stdout" in reason
    assert "CUDA 内存不足\n完整 traceback" in reason
    status = cv.setup_status(tmp_path)
    assert status["installed"] is True
    assert status["state"] == "validation_failed"
    assert status["retry_validation"] is True
    assert status["runtime_ok"] is True and status["models_ok"] is True
    assert status["reason"] == reason


def test_deep_smoke_requires_explicit_expected_device(
        tmp_path: Path, monkeypatch):
    target = cv.runtime_target("Windows", "AMD64")
    assert target is not None
    monkeypatch.setattr(cv, "runtime_target", lambda *_a, **_k: target)
    monkeypatch.setattr(cv, "runtime_files_ready", lambda _: True)
    monkeypatch.setattr(cv, "models_ready", lambda _: True)
    seen = {}

    def fake_run(_command, *, cwd, env, timeout, progress_cb):
        seen.update(env=env, timeout=timeout)
        return (
            cv.subprocess.CompletedProcess(
                [], 0, 'MEDIA_AGENT_JSON:{"ok": true, "device": "cpu"}\n', ""),
            3.0,
            False,
        )

    monkeypatch.setattr(cv, "_run_smoke_process", fake_run)
    ok, reason = cv.runtime_smoke(tmp_path, deep=True)

    assert seen["env"]["MEDIA_AGENT_TORCH_DEVICE"] == "cuda"
    assert seen["timeout"] == cv._GPU_SMOKE_TIMEOUT
    assert ok is False
    assert "未确认使用预期设备" in reason
    assert not cv._smoke_path(tmp_path).exists()


def test_deep_smoke_cache_fingerprints_runtime_model_and_source(
        tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cv, "runtime_files_ready", lambda _: True)
    monkeypatch.setattr(cv, "models_ready", lambda _: True)
    source = cv.runtime_source(tmp_path) / "cosyvoice" / "cli" / "cosyvoice.py"
    source.parent.mkdir(parents=True)
    source.write_text("first", encoding="utf-8")
    marker = cv._smoke_path(tmp_path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(cv._fingerprint(tmp_path)), encoding="utf-8")
    monkeypatch.setattr(
        cv, "_run_smoke_process",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("matching fingerprint must skip synthesis")))

    assert cv.runtime_smoke(tmp_path, deep=True) == (True, "")
    source.write_text("changed-source", encoding="utf-8")
    assert not cv._deep_smoke_ready(tmp_path)


def test_validation_retry_never_downloads_or_replaces_assets(
        tmp_path: Path, monkeypatch):
    target = cv.runtime_target("Windows", "AMD64")
    assert target is not None
    status = {
        "ready": False, "installed": True, "state": "validation_failed",
        "retry_validation": True, "installable": True, "runtime_ok": True,
        "models_ok": True, "smoke_ok": False, "accelerator": "CUDA",
        "performance_warning": "", "reason": "previous failure",
    }
    monkeypatch.setattr(cv, "setup_status", lambda _: status)
    monkeypatch.setattr(
        cv, "_install_runtime",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("runtime download must not run")))
    monkeypatch.setattr(
        cv, "download_models",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("model download must not run")))
    monkeypatch.setattr(
        cv, "runtime_smoke",
        lambda *_a, **_k: (False, "CUDA out of memory\n完整堆栈"))

    result = cv.run_setup(tmp_path, validation_only=True)

    assert result["ok"] is False
    assert result["installed"] is True
    assert "CUDA out of memory\n完整堆栈" in result["message"]


def test_smoke_process_reports_stage_and_elapsed(monkeypatch, tmp_path: Path):
    calls = 0

    class Process:
        returncode = 0

        def communicate(self, timeout=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise cv.subprocess.TimeoutExpired(
                    [], timeout,
                    output=b'MEDIA_AGENT_JSON:{"stage":"loading_model"}\n')
            return (
                'MEDIA_AGENT_JSON:{"stage":"loading_model"}\n'
                'MEDIA_AGENT_JSON:{"ok":true,"device":"cuda"}\n',
                "",
            )

    monkeypatch.setattr(cv.subprocess, "Popen", lambda *_a, **_k: Process())
    progress = []

    result, _elapsed, timed_out = cv._run_smoke_process(
        ["python"], cwd=tmp_path, env={}, timeout=60,
        progress_cb=lambda message, fraction: progress.append(
            (message, fraction)))

    assert timed_out is False and result.returncode == 0
    assert any("加载 CosyVoice2 模型" in message for message, _ in progress)
    assert all("已用时" in message for message, _ in progress)


def test_worker_audio_acceptance_rejects_silence_and_bad_duration():
    import numpy as np

    worker = runpy.run_path(str(
        Path(__file__).parents[1] / "packaging" / "cosyvoice_worker.py"))
    validate = worker["_validate_audio"]

    assert validate(np.zeros(16000, dtype=np.float32), 16000)[0] is False
    assert validate(np.full(100, .1, dtype=np.float32), 16000)[0] is False
    assert validate(np.full(8000, .1, dtype=np.float32), 16000)[0] is True
