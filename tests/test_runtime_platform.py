import os
from pathlib import Path

from app import runtime_platform as rp


def test_runtime_platform_matrix_and_assets():
    windows = rp.detect_runtime_platform("Windows", "AMD64")
    arm = rp.detect_runtime_platform("Darwin", "arm64")
    intel = rp.detect_runtime_platform("Darwin", "x86_64")

    assert windows and windows.device == "cuda"
    assert windows.python_relpath == Path("python.exe")
    assert arm and arm.device == "cpu" and arm.slow
    assert arm.python_relpath == Path("bin/python")
    assert intel and intel.key == "macos-x64-cpu"
    assert rp.runtime_asset("sadtalker", arm) == (
        "sadtalker-runtime-macos-arm64-cpu.tar.gz")
    assert rp.runtime_manifest("cosyvoice", intel) == (
        "cosyvoice-runtime-macos-x64-cpu.manifest.json")
    assert rp.detect_runtime_platform("Linux", "x86_64") is None


def test_prepare_runtime_tree_makes_unix_tools_executable(
        tmp_path, monkeypatch):
    target = rp.detect_runtime_platform("Darwin", "arm64")
    assert target is not None
    python = tmp_path / target.python_relpath
    unpack = tmp_path / target.conda_unpack_relpath
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n")
    unpack.write_text("#!/bin/sh\n")
    python.chmod(0o600)
    unpack.chmod(0o600)
    calls = []
    monkeypatch.setattr(
        rp.subprocess, "run",
        lambda *args, **kwargs: calls.append((args, kwargs)))

    rp.prepare_runtime_tree(tmp_path, target)

    if os.name != "nt":
        assert python.stat().st_mode & 0o111
        assert unpack.stat().st_mode & 0o111
    assert calls and calls[0][0][0][:3] == [
        "xattr", "-dr", "com.apple.quarantine"]
