from pathlib import Path
import io
import hashlib
import json
import tarfile

from app.video import sadtalker_setup as st


def test_model_urls_use_official_release_assets():
    url = st._model_url("checkpoints/SadTalker_V0.0.2_256.safetensors")
    assert url == (
        "https://github.com/OpenTalker/SadTalker/releases/download/"
        "v0.0.2-rc/SadTalker_V0.0.2_256.safetensors"
    )


def test_models_ready_rejects_partial_files(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(st, "REQUIRED_HASHES", {})
    model_dir = tmp_path / "models" / "sadtalker"
    for rel, expected in st.REQUIRED_FILES:
        p = model_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"404")
    assert not st.models_ready(tmp_path)

    # Sparse files avoid allocating ~1GB while testing strict size validation.
    for rel, expected in st.REQUIRED_FILES:
        p = model_dir / rel
        with p.open("wb") as f:
            f.truncate(expected)
    assert st.models_ready(tmp_path)


def test_run_setup_threads_mirror_to_downloader(tmp_path: Path, monkeypatch):
    seen = {}

    def fake_download(
        data_dir, *, mirror, proxy, progress_cb, cancel_event,
        pause_event=None,
    ):
        seen["mirror"] = mirror
        seen["proxy"] = proxy
        return True

    monkeypatch.setattr(st, "gpu_status",
                        lambda: {"ok": True, "name": "Test GPU", "reason": ""})
    monkeypatch.setattr(st, "_install_runtime", lambda *a, **k: None)
    monkeypatch.setattr(st, "download_models", fake_download)
    monkeypatch.setattr(st, "runtime_smoke",
                        lambda _, **_kwargs: (True, ""))
    monkeypatch.setattr(st, "setup_status", lambda _: {
        "ready": True, "gpu_ok": True, "runtime_ok": True,
        "models_ok": True, "smoke_ok": True, "reason": "",
    })
    monkeypatch.setattr(st, "resolve_sadtalker_dir", lambda _: "runtime/src")
    monkeypatch.setattr(st, "resolve_sadtalker_python",
                        lambda _: "runtime/python.exe")

    result = st.run_setup(tmp_path, mirror=False, proxy="http://127.0.0.1:7890")
    assert result["ok"] is True
    assert seen["mirror"] is False
    assert seen["proxy"] == "http://127.0.0.1:7890"


def test_status_does_not_report_ready_for_models_only(tmp_path: Path,
                                                       monkeypatch):
    monkeypatch.setattr(st, "REQUIRED_HASHES", {})
    model_dir = tmp_path / "models" / "sadtalker"
    for rel, expected in st.REQUIRED_FILES:
        path = model_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as stream:
            stream.truncate(expected)
    monkeypatch.setattr(st, "gpu_status",
                        lambda: {"ok": True, "name": "GPU", "reason": ""})

    status = st.setup_status(tmp_path)

    assert status["models_ok"] is True
    assert status["runtime_ok"] is False
    assert status["smoke_ok"] is False
    assert status["ready"] is False
    assert "runtime" in status["reason"]


def test_managed_runtime_resolution_never_uses_external_checkout(tmp_path: Path):
    assert st.resolve_sadtalker_dir(tmp_path) is None
    assert st.resolve_sadtalker_python(tmp_path) is None

    source = st.runtime_source(tmp_path)
    source.mkdir(parents=True)
    (source / "inference.py").write_text("", encoding="utf-8")
    st.runtime_python(tmp_path).write_bytes(b"python")
    st._runtime_version_path(tmp_path).write_text(
        st.RUNTIME_ASSET, encoding="utf-8")

    assert st.resolve_sadtalker_dir(tmp_path) == str(source)
    assert st.resolve_sadtalker_python(tmp_path) == str(
        st.runtime_python(tmp_path))


def test_status_requires_successful_real_inference_marker(tmp_path: Path,
                                                          monkeypatch):
    monkeypatch.setattr(st, "REQUIRED_HASHES", {})
    source = st.runtime_source(tmp_path)
    source.mkdir(parents=True)
    (source / "inference.py").write_text("", encoding="utf-8")
    st.runtime_python(tmp_path).write_bytes(b"python")
    st._runtime_version_path(tmp_path).write_text(
        st.RUNTIME_ASSET, encoding="utf-8")
    for rel, expected in st.REQUIRED_FILES:
        path = st._models_dir(tmp_path) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as stream:
            stream.truncate(expected)
    monkeypatch.setattr(st, "gpu_status",
                        lambda: {"ok": True, "name": "GPU", "reason": ""})
    monkeypatch.setattr(st, "runtime_smoke", lambda *_a, **_k: (True, ""))

    assert st.setup_status(tmp_path)["ready"] is False
    st._smoke_marker(tmp_path).write_text(
        json.dumps(st._smoke_fingerprint(tmp_path)),
        encoding="utf-8")
    status = st.setup_status(tmp_path)

    assert status["smoke_ok"] is True
    assert status["ready"] is True


def test_deep_smoke_failure_preserves_streams_and_status(
        tmp_path: Path, monkeypatch):
    rel = "checkpoints/model.bin"
    monkeypatch.setattr(st, "REQUIRED_FILES", [(rel, 3)])
    monkeypatch.setattr(st, "ALL_FILES", [(rel, 3)])
    monkeypatch.setattr(st, "REQUIRED_HASHES", {})
    source = st.runtime_source(tmp_path)
    (source / "examples" / "source_image").mkdir(parents=True)
    (source / "examples" / "driven_audio").mkdir(parents=True)
    (source / "inference.py").write_text("", encoding="utf-8")
    (source / "examples" / "source_image" / "full_body_1.png").write_bytes(
        b"image")
    (source / "examples" / "driven_audio" / "bus_chinese.wav").write_bytes(
        b"audio")
    st.runtime_python(tmp_path).write_bytes(b"python")
    st._runtime_version_path(tmp_path).write_text(
        st._runtime_asset(), encoding="utf-8")
    model = st._models_dir(tmp_path) / rel
    model.parent.mkdir(parents=True)
    model.write_bytes(b"123")
    monkeypatch.setattr(st, "accelerator_status", lambda _target=None: {
        "ok": True, "name": "Test GPU", "accelerator": "CUDA", "reason": "",
    })

    results = iter([
        st.subprocess.CompletedProcess([], 0, "imports ok\n", ""),
        st.subprocess.CompletedProcess([], 0, "usage\n", ""),
    ])
    monkeypatch.setattr(st.subprocess, "run", lambda *_a, **_k: next(results))
    monkeypatch.setattr(
        st, "_prepare_smoke_assets",
        lambda *_a, **_k: (
            source / "examples" / "source_image" / "full_body_1.png",
            source / "examples" / "driven_audio" / "bus_chinese.wav"))
    monkeypatch.setattr(
        st, "_run_smoke_process",
        lambda *_a, **_k: (
            st.subprocess.CompletedProcess(
                [], 1, "加载模型：中文输出\nsecond line\n",
                "CUDA 内存不足\ntrace detail\n"),
            12.5,
            False,
        ))

    ok, reason = st.runtime_smoke(tmp_path, deep=True)

    assert ok is False
    assert "加载模型：中文输出\nsecond line" in reason
    assert "CUDA 内存不足\ntrace detail" in reason
    status = st.setup_status(tmp_path)
    assert status["installed"] is True
    assert status["state"] == "validation_failed"
    assert status["retry_validation"] is True
    assert status["runtime_ok"] is True and status["models_ok"] is True
    assert status["reason"] == reason


def test_smoke_command_uses_fast_cuda_flags(tmp_path: Path):
    command = st._smoke_command(
        Path("python.exe"), Path("source"), Path("checkpoints"),
        Path("results"), Path("face.png"), Path("audio.wav"),
        device="cuda")

    assert command[:3] == ["python.exe", "-c", st._SMOKE_RUNNER]
    assert command[4] == "cuda"
    assert command[command.index("--preprocess") + 1] == "resize"
    assert command[command.index("--size") + 1] == "256"
    assert command[command.index("--batch_size") + 1] == "2"
    assert "--still" in command
    assert "--cpu" not in command
    assert "--enhancer" not in command
    assert "full" not in command


def test_smoke_command_uses_upstream_cpu_flag_on_macos():
    command = st._smoke_command(
        Path("bin/python"), Path("source"), Path("checkpoints"),
        Path("results"), Path("face.png"), Path("audio.wav"),
        device="cpu")

    assert command[4] == "cpu"
    assert "--cpu" in command
    assert "--device" not in command


def test_prepare_smoke_assets_creates_short_deterministic_media(
        tmp_path: Path):
    import wave
    from PIL import Image

    source = tmp_path / "source"
    face = source / "examples" / "source_image" / "happy.png"
    audio = source / "examples" / "driven_audio" / "bus_chinese.wav"
    face.parent.mkdir(parents=True)
    audio.parent.mkdir(parents=True)
    Image.new("RGB", (512, 320), "red").save(face)
    with wave.open(str(audio), "wb") as wav:
        wav.setparams((1, 2, 16000, 32000, "NONE", "not compressed"))
        wav.writeframes(b"\x01\x00" * 32000)

    smoke_face, smoke_audio = st._prepare_smoke_assets(
        source, tmp_path / "work")

    assert Image.open(smoke_face).size == (256, 256)
    with wave.open(str(smoke_audio), "rb") as wav:
        assert wav.getframerate() == 16000
        assert wav.getnframes() == 16000


def test_smoke_process_reports_stage_progress(monkeypatch, tmp_path: Path):
    calls = 0

    class Process:
        returncode = 0

        def communicate(self, timeout=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise st.subprocess.TimeoutExpired(
                    [], timeout, output=(
                        b"[media-agent] selected_device=cuda\n"
                        b"3DMM Extraction for source image\n"))
            return (
                "[media-agent] selected_device=cuda\n"
                "3DMM Extraction for source image\n"
                "The generated video is named: result.mp4\n",
                "",
            )

        def kill(self):
            raise AssertionError("healthy process should not be killed")

    monkeypatch.setattr(st.subprocess, "Popen", lambda *_a, **_k: Process())
    progress = []

    result, _elapsed, timed_out = st._run_smoke_process(
        ["python"], cwd=tmp_path, env={}, timeout=60,
        progress_cb=lambda message, fraction: progress.append(
            (message, fraction)))

    assert timed_out is False and result.returncode == 0
    assert any("提取单帧人脸参数" in message for message, _ in progress)
    assert all("已用时" in message for message, _ in progress)


def test_smoke_process_timeout_is_bounded_and_keeps_output(
        monkeypatch, tmp_path: Path):
    ticks = iter([0.0, 301.0, 302.0])

    class Process:
        returncode = -9
        killed = False

        def communicate(self, timeout=None):
            assert self.killed
            return ("checkpoint loading\n", "last CUDA diagnostic\n")

        def kill(self):
            self.killed = True

    monkeypatch.setattr(st.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(st.subprocess, "Popen", lambda *_a, **_k: Process())

    result, elapsed, timed_out = st._run_smoke_process(
        ["python"], cwd=tmp_path, env={}, timeout=300, progress_cb=None)

    assert timed_out is True
    assert elapsed == 302.0
    assert "checkpoint loading" in result.stdout
    assert "last CUDA diagnostic" in result.stderr


def test_smoke_process_grace_kills_after_stages_seen(
        monkeypatch, tmp_path: Path):
    """When all inference stages complete but process hangs, the grace
    period should kill it without marking it as timed out."""
    # Ticks consumed by time.monotonic():
    # 0: started (L461)
    # 1: iter1 top L491 (elapsed)
    # 2: iter1 except L505 (elapsed)
    # 3: iter2 top L491
    # 4: iter2 except L505 → fraction=0.7, all stages seen
    # 5: iter2 L518 stages_seen_at = time.monotonic()
    # 6: iter3 top L491 (remaining must be > 0)
    # 7: iter3 except L505 (elapsed)
    # 8: iter3 L519 grace check → must be > tick5 + 120
    # 9: post-loop L530 (elapsed = time.monotonic() - started)
    ticks = iter([0.0, 8.0, 16.0, 24.0, 32.0, 33.0, 154.0, 162.0, 260.0, 261.0])
    calls = 0
    stage1 = (
        b"[media-agent] selected_device=cuda\n"
        b"3DMM Extraction for source image\n")
    stage_all = (
        b"[media-agent] selected_device=cuda\n"
        b"3DMM Extraction for source image\n"
        b"mel: 100% 0:00:01\n"
        b"audio2exp:: 100% 0:00:05\n")

    class Process:
        returncode = 0
        killed = False

        def communicate(self, timeout=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise st.subprocess.TimeoutExpired(
                    [], timeout, output=stage1)
            if calls <= 3:
                raise st.subprocess.TimeoutExpired(
                    [], timeout, output=stage_all)
            # Post-kill: must have been killed by grace logic
            assert self.killed
            return (
                "[media-agent] selected_device=cuda\n"
                "3DMM Extraction for source image\n"
                "mel: 100% 0:00:01\n"
                "audio2exp:: 100% 0:00:05\n", "")

        def kill(self):
            self.killed = True

    monkeypatch.setattr(st.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(st.subprocess, "Popen", lambda *_a, **_k: Process())

    progress = []
    result, elapsed, timed_out = st._run_smoke_process(
        ["python"], cwd=tmp_path, env={}, timeout=300,
        progress_cb=lambda message, fraction: progress.append(
            (message, fraction)))

    # Process was killed by grace logic, NOT by the 300s timeout
    assert timed_out is False
    assert result.returncode == 0
    assert any("推理已完成" in m for m, _ in progress)


def test_deep_smoke_cache_skips_process_for_matching_fingerprint(
        tmp_path: Path, monkeypatch):
    monkeypatch.setattr(st, "runtime_files_ready", lambda _: True)
    marker = st._smoke_marker(tmp_path)
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps(st._smoke_fingerprint(tmp_path)), encoding="utf-8")
    monkeypatch.setattr(
        st.subprocess, "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("cached validation must not launch inference")))
    progress = []

    result = st.runtime_smoke(
        tmp_path, deep=True,
        progress_cb=lambda message, fraction: progress.append(
            (message, fraction)))

    assert result == (True, "")
    assert progress == [("已使用通过验证的 runtime 缓存", 1.0)]


def test_deep_smoke_rejects_silent_device_fallback(
        tmp_path: Path, monkeypatch):
    target = st.runtime_target("Windows", "AMD64")
    assert target is not None
    monkeypatch.setattr(st, "runtime_target", lambda *_a, **_k: target)
    monkeypatch.setattr(st, "runtime_files_ready", lambda _: True)
    python = st.runtime_python(tmp_path)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    results = iter([
        st.subprocess.CompletedProcess([], 0, "GPU ok", ""),
        st.subprocess.CompletedProcess([], 0, "usage", ""),
    ])
    monkeypatch.setattr(st.subprocess, "run", lambda *_a, **_k: next(results))

    def prepare(_source, input_dir):
        input_dir.mkdir(parents=True)
        (input_dir.parent / "result.mp4").write_bytes(b"video")
        return input_dir / "face.png", input_dir / "audio.wav"

    monkeypatch.setattr(st, "_prepare_smoke_assets", prepare)
    monkeypatch.setattr(st, "_run_smoke_process", lambda *_a, **_k: (
        st.subprocess.CompletedProcess(
            [], 0, "[media-agent] selected_device=cpu\n", ""),
        5.0,
        False,
    ))

    ok, reason = st.runtime_smoke(tmp_path, deep=True)

    assert ok is False
    assert "未确认使用预期设备" in reason
    assert not st._smoke_marker(tmp_path).exists()


def test_smoke_video_inspection_rejects_empty_output(
        tmp_path: Path, monkeypatch):
    response = st.subprocess.CompletedProcess(
        [], 0,
        json.dumps({
            "opened": True, "decoded": False, "frames": 0, "fps": 0,
            "width": 0, "height": 0, "std": 0,
        }),
        "",
    )
    monkeypatch.setattr(st.subprocess, "run", lambda *_a, **_k: response)

    ok, reason = st._inspect_smoke_video(
        Path("python"), tmp_path / "empty.mp4", cwd=tmp_path, env={})

    assert ok is False
    assert "验证视频内容无效" in reason


def test_validation_retry_skips_runtime_and_model_downloads(
        tmp_path: Path, monkeypatch):
    target = st.runtime_target("Windows", "AMD64")
    assert target is not None
    status = {
        "ready": False, "installed": True, "state": "validation_failed",
        "retry_validation": True, "runtime_ok": True, "models_ok": True,
        "smoke_ok": False, "reason": "previous failure",
    }
    monkeypatch.setattr(st, "runtime_target", lambda *_a, **_k: target)
    monkeypatch.setattr(st, "accelerator_status", lambda _target=None: {
        "ok": True, "name": "Test GPU", "accelerator": "CUDA", "reason": "",
    })
    monkeypatch.setattr(st, "setup_status", lambda _: status)
    monkeypatch.setattr(
        st, "_install_runtime",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("runtime download must not run")))
    monkeypatch.setattr(
        st, "download_models",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("model download must not run")))
    monkeypatch.setattr(
        st, "runtime_smoke",
        lambda *_a, **_k: (False, "CUDA out of memory\n完整堆栈"))

    result = st.run_setup(tmp_path, validation_only=True)

    assert result["ok"] is False
    assert result["installed"] is True
    assert "CUDA out of memory\n完整堆栈" in result["message"]


def test_models_ready_rejects_wrong_hash_at_exact_size(
        tmp_path: Path, monkeypatch):
    rel = "checkpoints/model.bin"
    payload = b"bad"
    path = st._models_dir(tmp_path) / rel
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    monkeypatch.setattr(st, "REQUIRED_FILES", [(rel, len(payload))])
    monkeypatch.setattr(st, "REQUIRED_HASHES", {
        rel: hashlib.sha256(b"good").hexdigest(),
    })

    assert not st.models_ready(tmp_path)


def test_model_urls_prefer_author_mirror_then_official():
    urls = st._model_urls("checkpoints/model.bin", mirror=True)

    assert urls[0] == (
        "https://hf-mirror.com/vinthony/SadTalker-V002rc/"
        "resolve/main/model.bin"
    )
    assert st._model_url("checkpoints/model.bin", mirror=False) in urls


def test_safe_extract_rejects_path_traversal(tmp_path: Path):
    archive = tmp_path / "runtime.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("../escape.txt")
        payload = b"bad"
        info.size = len(payload)
        bundle.addfile(info, io.BytesIO(payload))

    try:
        st._safe_extract(archive, tmp_path / "runtime")
    except RuntimeError as exc:
        assert "不安全路径" in str(exc)
    else:
        raise AssertionError("path traversal archive was accepted")


def test_safe_extract_rejects_escaping_symlink(tmp_path: Path):
    archive = tmp_path / "runtime-link.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("bin/python")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../outside"
        bundle.addfile(info)

    try:
        st._safe_extract(archive, tmp_path / "runtime")
    except RuntimeError as exc:
        assert "不安全链接" in str(exc)
    else:
        raise AssertionError("escaping symlink was accepted")


def test_local_archive_installs_to_release_managed_layout(tmp_path: Path):
    payload = tmp_path / "payload"
    (payload / "sadtalker-src").mkdir(parents=True)
    (payload / "python.exe").write_bytes(b"python")
    (payload / "sadtalker-src" / "inference.py").write_text(
        "", encoding="utf-8")
    archive = tmp_path / st.RUNTIME_ASSET
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(payload / "python.exe", arcname="python.exe")
        bundle.add(payload / "sadtalker-src",
                   arcname="sadtalker-src")

    data_dir = tmp_path / "data"
    st.install_runtime_archive(data_dir, archive)

    assert st.runtime_files_ready(data_dir)
    assert st.resolve_sadtalker_python(data_dir) == str(
        data_dir / "runtimes" / "sadtalker" / "python.exe")
    assert st.resolve_sadtalker_dir(data_dir) == str(
        data_dir / "runtimes" / "sadtalker" / "sadtalker-src")
    assert archive.exists()  # local build artifact is reusable


def test_local_archive_recovers_already_extracted_runtime(tmp_path: Path):
    data_dir = tmp_path / "data"
    target = st.runtime_dir(data_dir)
    (target / "sadtalker-src").mkdir(parents=True)
    (target / "python.exe").write_bytes(b"python")
    (target / "sadtalker-src" / "inference.py").write_text(
        "", encoding="utf-8")
    archive = tmp_path / st.RUNTIME_ASSET
    archive.write_bytes(b"already extracted")
    messages = []

    st.install_runtime_archive(
        data_dir, archive,
        progress_cb=lambda message, _fraction: messages.append(message))

    assert st.runtime_files_ready(data_dir)
    assert any("继续完成激活" in message for message in messages)


def test_runtime_marker_not_written_when_conda_unpack_fails(
        tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "data"
    target = st.runtime_dir(data_dir)
    (target / "sadtalker-src").mkdir(parents=True)
    (target / "Scripts").mkdir()
    (target / "python.exe").write_bytes(b"python")
    (target / "sadtalker-src" / "inference.py").write_text(
        "", encoding="utf-8")
    (target / "Scripts" / "conda-unpack.exe").write_bytes(b"unpack")
    archive = tmp_path / st.RUNTIME_ASSET
    archive.write_bytes(b"already extracted")

    class Result:
        returncode = 1
        stderr = "relocation failed"
        stdout = ""

    monkeypatch.setattr(st.subprocess, "run", lambda *_a, **_k: Result())

    try:
        st.install_runtime_archive(data_dir, archive)
    except RuntimeError as exc:
        assert "relocation failed" in str(exc)
    else:
        raise AssertionError("failed conda-unpack was accepted")

    assert not st._runtime_version_path(data_dir).exists()
    assert not st.runtime_files_ready(data_dir)


def test_runtime_part_download_resumes_with_http_range(tmp_path: Path):
    destination = tmp_path / "runtime.part001"
    destination.write_bytes(b"abc")
    expected = b"abcdef"

    class Response:
        status_code = 206

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self, _size):
            yield b"def"

    class Client:
        headers = None

        def stream(self, _method, _url, headers):
            self.headers = headers
            return Response()

    client = Client()
    st._download_runtime_part(
        client, url="https://example/runtime.part001",
        destination=destination, expected_size=len(expected),
        expected_sha=hashlib.sha256(expected).hexdigest(),
        completed_bytes=0, total_bytes=len(expected),
        emit=lambda *_: None, cancel_event=None,
    )

    assert client.headers == {"Range": "bytes=3-"}
    assert destination.read_bytes() == expected


def test_run_setup_stops_before_download_without_nvidia(tmp_path: Path,
                                                        monkeypatch):
    monkeypatch.setattr(st, "gpu_status", lambda: {
        "ok": False, "name": "", "reason": "no NVIDIA",
    })
    monkeypatch.setattr(st, "setup_status", lambda _: {
        "ready": False, "gpu_ok": False, "runtime_ok": False,
        "models_ok": False, "smoke_ok": False, "reason": "no NVIDIA",
    })
    called = []
    monkeypatch.setattr(st, "_install_runtime",
                        lambda *a, **k: called.append(True))

    result = st.run_setup(tmp_path)

    assert result["ok"] is False
    assert "NVIDIA" in result["message"]
    assert called == []


def test_macos_runtime_targets_cover_arm_and_intel():
    arm = st.runtime_target("Darwin", "arm64")
    intel = st.runtime_target("Darwin", "x86_64")

    assert arm and arm.key == "macos-arm64-cpu"
    assert intel and intel.key == "macos-x64-cpu"
    assert arm.python_relpath == Path("bin/python")
    assert intel.requires_gpu is False
    assert st._runtime_asset(arm) == (
        "sadtalker-runtime-macos-arm64-cpu.tar.gz")
    assert st._runtime_asset(intel) == (
        "sadtalker-runtime-macos-x64-cpu.tar.gz")


def test_macos_cpu_status_does_not_probe_nvidia(tmp_path: Path, monkeypatch):
    target = st.runtime_target("Darwin", "arm64")
    assert target is not None
    monkeypatch.setattr(st, "runtime_target", lambda *_a, **_k: target)
    monkeypatch.setattr(
        st, "gpu_status",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected GPU probe")))

    status = st.setup_status(tmp_path)

    assert status["supported"] is True
    assert status["installable"] is True
    assert status["accelerator"] == "CPU"
    assert status["gpu_ok"] is False
    assert "CPU" in status["performance_warning"]
    assert st.runtime_python(tmp_path) == (
        tmp_path / "runtimes" / "sadtalker" / "bin" / "python")
