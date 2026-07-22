"""CosyVoice provider backed by the managed standalone runtime."""
from __future__ import annotations

import atexit
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

from app.cosyvoice_install import (
    model_dir, runtime_dir, runtime_files_ready, runtime_python,
    runtime_worker, models_ready, runtime_target,
)

_PROCESS: subprocess.Popen[str] | None = None
_PROCESS_KEY: tuple[str, str] | None = None
_LOCK = threading.RLock()


def _stop_worker() -> None:
    global _PROCESS, _PROCESS_KEY
    with _LOCK:
        process = _PROCESS
        _PROCESS = None
        _PROCESS_KEY = None
        if not process:
            return
        try:
            if process.poll() is None and process.stdin:
                process.stdin.write('{"command":"shutdown"}\n')
                process.stdin.flush()
                process.wait(timeout=10)
        except Exception:
            process.kill()


atexit.register(_stop_worker)


def _read_response(process: subprocess.Popen[str]) -> dict[str, Any]:
    assert process.stdout is not None
    detail = ""
    for line in process.stdout:
        detail = (detail + line)[-2000:]
        if not line.startswith("MEDIA_AGENT_JSON:"):
            continue
        try:
            return json.loads(line.split(":", 1)[1])
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"CosyVoice worker 返回无效数据：{line[-500:]}") from exc
    raise RuntimeError(
        "CosyVoice worker 已退出" + (f"：{detail}" if detail else ""))


def _worker(data_dir: Path) -> subprocess.Popen[str]:
    global _PROCESS, _PROCESS_KEY
    key = (str(runtime_python(data_dir)), str(model_dir(data_dir)))
    if (_PROCESS is not None and _PROCESS.poll() is None
            and _PROCESS_KEY == key):
        return _PROCESS
    _stop_worker()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(runtime_dir(data_dir) / "cosyvoice-src")
    target = runtime_target()
    if target is not None and not target.requires_gpu:
        env["MEDIA_AGENT_TORCH_DEVICE"] = "cpu"
    process = subprocess.Popen(
        [
            str(runtime_python(data_dir)), "-u", str(runtime_worker(data_dir)),
            "serve", "--model-dir", str(model_dir(data_dir)),
        ],
        cwd=str(runtime_dir(data_dir)), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)
    ready = _read_response(process)
    if not ready.get("ok"):
        process.kill()
        raise RuntimeError(str(ready.get("error") or "CosyVoice 启动失败"))
    _PROCESS = process
    _PROCESS_KEY = key
    return process


class CosyVoiceRuntimeTTS:
    """Zero-shot voice cloning through the managed worker process."""

    def __init__(self, *, data_dir: Path, speaker_wav: str | None = None,
                 instruct: str | None = None):
        self.data_dir = Path(data_dir)
        self.speaker_wav = speaker_wav
        self.instruct = (instruct or "").strip()
        self._prompt_text = ""

    def synthesize(self, text: str, out_stem: Path) -> Path:
        if not self.speaker_wav or not Path(self.speaker_wav).is_file():
            raise RuntimeError(
                "未选择声音样本：请在「设置 → 声音库」录入并选用一个声音")
        if not runtime_files_ready(self.data_dir) or not models_ready(
                self.data_dir):
            raise RuntimeError(
                "CosyVoice 尚未就绪，请在「设置 → 语音与视频」下载安装 Runtime 与模型")
        output = Path(out_stem).with_suffix(".wav").resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        request = {
            "command": "synthesize",
            "text": text,
            "prompt_wav": str(Path(self.speaker_wav).resolve()),
            "prompt_text": self._prompt_text,
            "instruct": self.instruct,
            "output": str(output),
        }
        with _LOCK:
            process = _worker(self.data_dir)
            assert process.stdin is not None
            try:
                process.stdin.write(json.dumps(
                    request, ensure_ascii=False) + "\n")
                process.stdin.flush()
                response = _read_response(process)
            except Exception:
                _stop_worker()
                raise
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error") or
                                   "CosyVoice 合成失败"))
        self._prompt_text = str(response.get("prompt_text") or
                                self._prompt_text)
        result = Path(str(response.get("output") or output))
        if not result.is_file():
            raise RuntimeError("CosyVoice 推理未产生输出文件")
        return result
