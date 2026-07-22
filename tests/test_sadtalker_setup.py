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


def test_models_ready_rejects_partial_files(tmp_path: Path):
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

    def fake_download(data_dir, *, mirror, proxy, progress_cb, cancel_event):
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
