"""CosyVoice TTS backed by a prebuilt runtime (conda-pack env) via subprocess.

Unlike :mod:`app.tts.providers.cosyvoice` (which imports torch/cosyvoice into
media-agent's own interpreter), this provider talks to a separate, self-contained
CosyVoice runtime shipped with the release. That runtime has all native deps
(torch/torchaudio/onnxruntime/pynini) prebuilt, so the user never compiles
anything. We spawn ``packaging/cosyvoice_worker.py`` once with the runtime's
python and stream synthesis requests to it (model loads once, reused per scene).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


def worker_script() -> Path:
    """Locate the standalone worker script (bundled when frozen)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        p = Path(sys._MEIPASS) / "packaging" / "cosyvoice_worker.py"
        if p.exists():
            return p
    return Path(__file__).resolve().parents[3] / "packaging" / "cosyvoice_worker.py"


def runtime_python(runtime_dir: str) -> Path | None:
    """Return the runtime env's python executable, or None."""
    d = Path(runtime_dir)
    for c in (d / "python.exe", d / "python3.exe",
              d / "bin" / "python", d / "bin" / "python3"):
        if c.exists():
            return c
    return None


def default_runtime_dir() -> str | None:
    """Well-known locations where setup-optional extracts the runtime.

    Checked when no explicit ``cosyvoice_runtime_dir`` is configured, so a
    download-and-extract install works with zero settings.
    """
    roots: list[Path] = [Path.cwd()]
    if getattr(sys, "frozen", False):
        roots.insert(0, Path(sys.executable).resolve().parent)
    for root in roots:
        cand = root / "_cosyvoice-runtime"
        if runtime_python(str(cand)):
            return str(cand)
    return None


def resolve_model_dir(runtime_dir: str, model: str | None) -> str | None:
    if model:
        p = Path(model)
        if p.exists():
            return str(p.resolve())
    for c in (Path(runtime_dir) / "pretrained_models" / "CosyVoice2-0.5B",
              Path(runtime_dir).parent / "pretrained_models" / "CosyVoice2-0.5B",
              Path.cwd() / "pretrained_models" / "CosyVoice2-0.5B"):
        if c.exists():
            return str(c.resolve())
    return None


def runtime_available(runtime_dir: str | None) -> bool:
    runtime_dir = runtime_dir or default_runtime_dir()
    return bool(runtime_dir and runtime_python(runtime_dir) and worker_script().exists())


class CosyVoiceRuntimeTTS:
    def __init__(self, runtime_dir: str | None = None, speaker_wav: str | None = None,
                 model: str | None = None, instruct: str | None = None):
        self.runtime_dir = runtime_dir or default_runtime_dir() or ""
        self.speaker_wav = speaker_wav
        self.instruct = (instruct or "").strip()
        self.model_dir = resolve_model_dir(runtime_dir, model)
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._rid = 0

    def _ensure_worker(self) -> None:
        if self._proc and self._proc.poll() is None:
            return
        py = runtime_python(self.runtime_dir)
        if not py:
            raise RuntimeError(
                f"CosyVoice 运行时无效（未找到 python）：{self.runtime_dir}")
        script = worker_script()
        if not script.exists():
            raise RuntimeError(f"未找到 CosyVoice worker 脚本：{script}")
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUNBUFFERED", "1")
        logger.info("启动 CosyVoice 运行时 worker：%s", py)
        self._proc = subprocess.Popen(
            [str(py), "-u", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
            text=True, encoding="utf-8", cwd=str(Path(self.runtime_dir)),
            env=env)
        # Wait for the worker's readiness banner.
        assert self._proc.stdout is not None
        while True:
            line = self._proc.stdout.readline()
            if not line:
                self._proc = None
                raise RuntimeError("CosyVoice worker 启动失败（未就绪）")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("event") == "ready":
                logger.info("CosyVoice worker 就绪")
                return

    def synthesize(self, text: str, out_stem: Path) -> Path:
        if not self.model_dir:
            raise RuntimeError(
                "未找到 CosyVoice 模型目录（首次使用请先下载 CosyVoice2-0.5B 模型）")
        if not self.speaker_wav or not Path(self.speaker_wav).exists():
            raise RuntimeError(
                "未选择声音样本：请在「设置 → 声音库」录入并选用一个声音")
        out = Path(out_stem).with_suffix(".wav")
        out.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._ensure_worker()
            assert self._proc is not None and self._proc.stdin is not None
            assert self._proc.stdout is not None
            self._rid += 1
            rid = self._rid
            req = {"id": rid, "text": text, "out_stem": str(out_stem),
                   "ref": str(self.speaker_wav), "model": self.model_dir,
                   "instruct": self.instruct}
            self._proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
            while True:
                line = self._proc.stdout.readline()
                if not line:
                    self._proc = None
                    raise RuntimeError("CosyVoice worker 意外退出（查看日志）")
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") != rid:
                    continue
                if msg.get("ok"):
                    return Path(msg["path"])
                raise RuntimeError("CosyVoice 合成失败：" + str(msg.get("error")))

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                assert self._proc.stdin is not None
                self._proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                self._proc.stdin.flush()
                self._proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                self._proc.kill()
        self._proc = None
