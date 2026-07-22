"""Standalone worker shipped inside the managed CosyVoice runtime.

The main application intentionally never imports torch/CosyVoice.  Requests are
JSON objects on stdin and responses are JSON objects on stdout, one per line.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path


def _emit(payload: dict) -> None:
    print("MEDIA_AGENT_JSON:" + json.dumps(
        payload, ensure_ascii=False), flush=True)


def _install_meta_patch() -> None:
    import torch

    if getattr(torch.nn.Module, "_media_agent_cosyvoice_patch", False):
        return
    original_to = torch.nn.Module.to
    original_load = torch.nn.Module.load_state_dict

    def patched_to(self, *args, **kwargs):
        try:
            return original_to(self, *args, **kwargs)
        except RuntimeError as exc:
            if "meta tensor" not in str(exc) and "to_empty" not in str(exc):
                raise
            device = args[0] if args else kwargs.get("device", "cuda")
            return self.to_empty(device=device)

    def patched_load(self, state_dict, strict=True, assign=False):
        if not assign:
            try:
                assign = next(self.parameters()).device.type == "meta"
            except StopIteration:
                pass
        return original_load(self, state_dict, strict=strict, assign=assign)

    torch.nn.Module.to = patched_to
    torch.nn.Module.load_state_dict = patched_load
    torch.nn.Module._media_agent_cosyvoice_patch = True


class Engine:
    def __init__(self, model_dir: Path):
        import torch
        from cosyvoice.cli.cosyvoice import CosyVoice2

        if not torch.cuda.is_available():
            raise RuntimeError("CUDA unavailable in CosyVoice runtime")
        _install_meta_patch()
        self.torch = torch
        self.model = CosyVoice2(
            str(model_dir), load_jit=False, load_trt=False, fp16=True)
        self.whisper = None

    def transcribe(self, wav: str) -> str:
        if self.whisper is None:
            import whisper
            self.whisper = whisper.load_model("tiny")
        result = self.whisper.transcribe(
            wav, language="zh", task="transcribe", verbose=None, fp16=False)
        return str(result.get("text") or "").strip()

    def synthesize(self, request: dict) -> dict:
        import soundfile as sf

        output = Path(request["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        prompt_wav = str(Path(request["prompt_wav"]).resolve())
        prompt_text = str(request.get("prompt_text") or "").strip()
        if not prompt_text:
            prompt_text = self.transcribe(prompt_wav)
        instruct = str(request.get("instruct") or "").strip()
        if instruct and instruct not in prompt_text:
            prompt_text = f"{instruct}，{prompt_text}"
        results = self.model.inference_zero_shot(
            tts_text=str(request["text"]), prompt_text=prompt_text,
            prompt_wav=prompt_wav, stream=False)
        wrote = False
        for result in results:
            audio = result["tts_speech"].detach().cpu().squeeze(0).numpy()
            sf.write(str(output), audio, self.model.sample_rate)
            wrote = True
        if not wrote or not output.is_file():
            raise RuntimeError("CosyVoice did not produce audio")
        return {"ok": True, "output": str(output), "prompt_text": prompt_text}


def serve(model_dir: Path) -> int:
    engine = Engine(model_dir)
    _emit({"ok": True, "event": "ready"})
    for line in sys.stdin:
        try:
            request = json.loads(line)
            command = request.get("command")
            if command == "synthesize":
                _emit(engine.synthesize(request))
            elif command == "ping":
                _emit({"ok": True, "event": "pong"})
            elif command == "shutdown":
                _emit({"ok": True})
                return 0
            else:
                _emit({"ok": False, "error": f"unknown command: {command}"})
        except Exception as exc:
            _emit({"ok": False, "error": str(exc),
                   "traceback": traceback.format_exc()[-2000:]})
    return 0


def download_model(model_dir: Path, repo: str) -> int:
    from huggingface_hub import HfApi, snapshot_download
    from tqdm.auto import tqdm

    info = HfApi().model_info(repo, files_metadata=True)
    download_total = sum(
        int(getattr(file, "size", 0) or 0) for file in info.siblings)
    bars: dict[int, float] = {}

    class JsonProgress(tqdm):
        def display(self, msg=None, pos=None):
            del msg, pos
            total = float(self.total or 0)
            if total > 0 and getattr(self, "unit", "") == "B":
                bars[id(self)] = min(float(self.n), total)
                downloaded = sum(bars.values())
                _emit({
                    "event": "progress",
                    "message": f"下载 CosyVoice2 模型："
                               f"{downloaded / 1048576:.1f}/"
                               f"{download_total / 1048576:.1f} MB",
                    "fraction": min(
                        downloaded / max(download_total, 1), .99),
                })

    model_dir.mkdir(parents=True, exist_ok=True)
    _emit({"event": "progress", "message": "下载 CosyVoice2 模型…",
           "fraction": 0.0})
    snapshot_download(
        repo_id=repo, local_dir=str(model_dir), max_workers=2,
        local_dir_use_symlinks=False,
        tqdm_class=JsonProgress,
    )
    marker = {
        "repo": repo,
        "files": {
            str(path.relative_to(model_dir)).replace("\\", "/"):
                path.stat().st_size
            for path in model_dir.rglob("*") if path.is_file()
            and ".cache" not in path.parts
        },
    }
    (model_dir / ".model-ready.json").write_text(
        json.dumps(marker, indent=2), encoding="utf-8")
    _emit({"event": "progress", "message": "CosyVoice2 模型下载完成",
           "fraction": 1.0})
    return 0


def smoke(model_dir: Path, deep: bool = False) -> int:
    import tempfile
    import torch
    import onnxruntime
    import soundfile
    import whisper
    from cosyvoice.cli.cosyvoice import CosyVoice2

    del onnxruntime, soundfile, whisper, CosyVoice2
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA unavailable in CosyVoice runtime")
    synthesized = False
    if deep:
        engine = Engine(model_dir)
        sample = Path(__file__).resolve().parent / "cosyvoice-src" / "asset" / \
            "zero_shot_prompt.wav"
        if sample.is_file():
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "smoke.wav"
                engine.synthesize({
                    "text": "你好，这是声音合成运行验证。",
                    "prompt_wav": str(sample),
                    "prompt_text": "希望你以后能够做的比我还好呦。",
                    "output": str(output),
                })
                synthesized = output.stat().st_size > 44
    _emit({"ok": True, "gpu": torch.cuda.get_device_name(0), "deep": deep,
           "synthesized": synthesized})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    dl = sub.add_parser("download-model")
    dl.add_argument("--model-dir", required=True, type=Path)
    dl.add_argument("--repo", default="FunAudioLLM/CosyVoice2-0.5B")
    sm = sub.add_parser("smoke")
    sm.add_argument("--model-dir", required=True, type=Path)
    sm.add_argument("--deep", action="store_true")
    sv = sub.add_parser("serve")
    sv.add_argument("--model-dir", required=True, type=Path)
    args = parser.parse_args()

    source = Path(__file__).resolve().parent / "cosyvoice-src"
    if source.is_dir():
        sys.path.insert(0, str(source))
        os.environ["PYTHONPATH"] = str(source)
    if args.command == "download-model":
        return download_model(args.model_dir, args.repo)
    if args.command == "smoke":
        return smoke(args.model_dir, args.deep)
    return serve(args.model_dir)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        _emit({"ok": False, "error": str(exc),
               "traceback": traceback.format_exc()[-3000:]})
        raise SystemExit(1)
