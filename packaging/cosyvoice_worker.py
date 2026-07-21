"""Standalone CosyVoice synthesis worker.

Runs INSIDE the prebuilt CosyVoice runtime (a conda-pack'd environment shipped
with the release), NOT inside media-agent's own interpreter. media-agent spawns
this once as a long-lived subprocess and talks to it over line-delimited JSON on
stdin/stdout, so the (slow) model load happens only once and every scene reuses
it.

Protocol (one JSON object per line):
  parent → worker:  {"id": 1, "text": "...", "out_stem": "C:/.../scene-1",
                     "ref": "C:/.../voice.wav", "model": "C:/.../CosyVoice2-0.5B",
                     "instruct": "用亲切的语气"}
                    {"cmd": "quit"}
  worker → parent:  {"event": "ready"}                       # after imports OK
                    {"id": 1, "ok": true, "path": "C:/.../scene-1.wav"}
                    {"id": 1, "ok": false, "error": "..."}

All library/log noise is redirected to stderr so stdout carries only protocol
JSON. This file must stay dependency-free of the media-agent package (it runs in
a different interpreter).
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
import traceback
from pathlib import Path

# Protocol channel: keep the *real* stdout for JSON only; send everything else
# (torch/cosyvoice prints, warnings) to stderr so it can't corrupt the stream.
_PROTO = sys.stdout
sys.stdout = sys.stderr

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="[cosyvoice-worker] %(levelname)s %(message)s")
logger = logging.getLogger("cosyvoice_worker")


def emit(obj: dict) -> None:
    _PROTO.write(json.dumps(obj, ensure_ascii=False) + "\n")
    _PROTO.flush()


# --- torch meta-device patch (CosyVoice2 + torch>=2.4) ---------------------
def _install_meta_device_patch() -> None:
    import torch
    if getattr(torch.nn.Module, "_cosyvoice_patched", False):
        return
    _orig_to = torch.nn.Module.to

    def _patched_to(self, *args, **kwargs):
        try:
            return _orig_to(self, *args, **kwargs)
        except RuntimeError as e:
            if "meta tensor" in str(e) or "to_empty" in str(e):
                device = args[0] if args else kwargs.get("device", "cuda")
                return self.to_empty(device)
            raise

    torch.nn.Module.to = _patched_to
    _orig_lsd = torch.nn.Module.load_state_dict

    def _patched_lsd(self, state_dict, strict=True, assign=False):
        if not assign:
            try:
                if next(self.parameters()).device.type == "meta":
                    assign = True
            except StopIteration:
                pass
        return _orig_lsd(self, state_dict, strict=strict, assign=assign)

    torch.nn.Module.load_state_dict = _patched_lsd
    torch.nn.Module._cosyvoice_patched = True


# --- global caches (load once, reuse across requests) ----------------------
_MODEL = None
_WHISPER = None
# per reference-audio caches
_REF_WAV: dict[str, str] = {}
_PROMPT_TEXT: dict[str, str] = {}
_SPK_ID: dict[str, str] = {}


def _get_model(model_dir: str):
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    _install_meta_device_patch()
    import torch
    from cosyvoice.cli.cosyvoice import CosyVoice2
    fp16 = torch.cuda.is_available()
    logger.info("loading CosyVoice2 model %s (fp16=%s)", model_dir, fp16)
    _MODEL = CosyVoice2(model_dir, load_jit=False, load_trt=False, fp16=fp16)
    logger.info("model loaded")
    return _MODEL


def _get_whisper():
    global _WHISPER
    if _WHISPER is not None:
        return _WHISPER
    import whisper
    logger.info("loading whisper tiny (reference transcription)")
    _WHISPER = whisper.load_model("tiny")
    return _WHISPER


def _resolve_ref_wav(speaker_wav: str) -> str:
    if speaker_wav in _REF_WAV:
        return _REF_WAV[speaker_wav]
    p = Path(speaker_wav).resolve()
    if p.suffix.lower() == ".wav":
        _REF_WAV[speaker_wav] = str(p)
        return str(p)
    cached = p.with_name(f"{p.stem}_16k.wav")
    if not cached.exists():
        from pydub import AudioSegment
        audio = AudioSegment.from_file(str(p))
        audio = audio.set_frame_rate(16000).set_sample_width(2)
        audio.export(str(cached), format="wav")
        logger.info("converted reference %s -> %s", p, cached)
    _REF_WAV[speaker_wav] = str(cached)
    return str(cached)


def _transcribe_ref(wav_path: str) -> str:
    try:
        import torch
        from contextlib import nullcontext
        model = _get_whisper()
        try:
            sdp = torch.backends.cuda.sdp_kernel(
                enable_flash=False, enable_mem_efficient=False, enable_math=True)
        except AttributeError:
            sdp = nullcontext()
        with sdp:
            result = model.transcribe(wav_path, language="zh", task="transcribe",
                                      verbose=None, fp16=False)
        return (result.get("text") or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("reference transcription failed: %s", e)
        return ""


def _ensure_spk(model, ref_wav: str) -> tuple[str, str]:
    """Return (spk_id, prompt_text), caching per reference audio."""
    if ref_wav in _SPK_ID:
        return _SPK_ID[ref_wav], _PROMPT_TEXT.get(ref_wav, "")
    prompt_text = _transcribe_ref(ref_wav)
    _PROMPT_TEXT[ref_wav] = prompt_text
    spk_id = "spk_" + hashlib.md5(ref_wav.encode()).hexdigest()[:12]
    try:
        model.add_zero_shot_spk(prompt_text=prompt_text, prompt_wav=ref_wav,
                                zero_shot_spk_id=spk_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("add_zero_shot_spk failed, per-call cloning: %s", e)
        spk_id = ""
    _SPK_ID[ref_wav] = spk_id
    return spk_id, prompt_text


def _synth(req: dict) -> str:
    text = req["text"]
    out_stem = req["out_stem"]
    ref = req["ref"]
    model_dir = req["model"]
    instruct = (req.get("instruct") or "").strip()

    if not ref or not Path(ref).exists():
        raise RuntimeError("reference audio missing")
    out = Path(out_stem).with_suffix(".wav")
    out.parent.mkdir(parents=True, exist_ok=True)

    import soundfile as sf
    model = _get_model(model_dir)
    ref_wav = _resolve_ref_wav(ref)
    spk_id, prompt_text = _ensure_spk(model, ref_wav)
    if instruct and instruct not in prompt_text:
        prompt_text = instruct + "，" + prompt_text

    for result in model.inference_zero_shot(
            tts_text=text, prompt_text=prompt_text, prompt_wav=ref_wav,
            zero_shot_spk_id=spk_id, stream=False):
        audio = result["tts_speech"]
        sf.write(str(out), audio.squeeze(0).numpy(), model.sample_rate)
    if not out.exists():
        raise RuntimeError("no output produced")
    return str(out)


def main() -> int:
    emit({"event": "ready"})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if req.get("cmd") == "quit":
            break
        rid = req.get("id")
        try:
            path = _synth(req)
            emit({"id": rid, "ok": True, "path": path})
        except Exception as e:  # noqa: BLE001
            logger.error("synth failed: %s\n%s", e, traceback.format_exc())
            emit({"id": rid, "ok": False, "error": str(e)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
