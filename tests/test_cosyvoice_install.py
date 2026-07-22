from __future__ import annotations

import hashlib
import io
import json
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
