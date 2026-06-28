from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

from app.config import Config

# Local registry of cloned voices: a reference audio sample + metadata, used
# as the speaker reference for voice-cloning TTS (e.g. Coqui XTTS-v2).

_ALLOWED_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm"}


def _registry_path(config: Config) -> Path:
    return config.voices_dir / "voices.json"


def _load(config: Config) -> dict:
    p = _registry_path(config)
    if not p.exists():
        return {"voices": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"voices": []}


def _save(config: Config, data: dict) -> None:
    config.voices_dir.mkdir(parents=True, exist_ok=True)
    _registry_path(config).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_voices(config: Config) -> list[dict]:
    return _load(config).get("voices", [])


def get_voice(config: Config, voice_id: str) -> dict | None:
    for v in list_voices(config):
        if v.get("id") == voice_id:
            return v
    return None


def sample_path(config: Config, voice_id: str | None) -> Path | None:
    if not voice_id:
        return None
    v = get_voice(config, voice_id)
    if not v or not v.get("sample"):
        return None
    p = (config.voices_dir / v["sample"]).resolve()
    base = config.voices_dir.resolve()
    if base in p.parents and p.exists():
        return p
    return None


def _ensure_16k_ref(config: Config, voice_id: str, filename: str) -> None:
    """Best-effort: convert a non-WAV voice sample to 16kHz/16bit WAV
    and save alongside as ``{voice_id}_16k.wav``.

    CosyVoice (``_resolve_ref_wav``) checks for this cached variant first,
    avoiding re-conversion on every ``synthesize()`` call.  If conversion
    fails (pydub/ffmpeg missing) it is a no-op — the TTS provider will
    convert lazily on first use.
    """
    ext = Path(filename or "").suffix.lower()
    if ext == ".wav":
        return
    src = config.voices_dir / f"{voice_id}{ext}"
    dst = config.voices_dir / f"{voice_id}_16k.wav"
    if dst.exists():
        return
    try:
        from pydub import AudioSegment
        audio = AudioSegment.from_file(str(src))
        audio = audio.set_frame_rate(16000).set_sample_width(2)
        audio.export(str(dst), format="wav")
        logger = __import__("logging").getLogger(__name__)
        logger.info("上传时已缓存 16kHz/16bit 参考音频 %s", dst)
    except Exception:
        pass  # non-fatal — cosyvoice provider will convert lazily


def add_voice(config: Config, name: str, data: bytes, filename: str) -> dict:
    name = (name or "未命名声音").strip()
    reg = _load(config)
    # Check for duplicate name
    for v in reg.get("voices", []):
        if v.get("name") == name:
            raise ValueError(f"声音名称「{name}」已存在")
    ext = Path(filename or "").suffix.lower()
    if ext not in _ALLOWED_EXTS:
        ext = ".wav"
    voice_id = secrets.token_hex(6)
    config.voices_dir.mkdir(parents=True, exist_ok=True)
    sample = f"{voice_id}{ext}"
    (config.voices_dir / sample).write_bytes(data)
    _ensure_16k_ref(config, voice_id, filename)
    entry = {
        "id": voice_id,
        "name": name,
        "sample": sample,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    reg.setdefault("voices", []).append(entry)
    _save(config, reg)
    return entry


def delete_voice(config: Config, voice_id: str) -> bool:
    reg = _load(config)
    voices = reg.get("voices", [])
    kept = [v for v in voices if v.get("id") != voice_id]
    if len(kept) == len(voices):
        return False
    for v in voices:
        if v.get("id") == voice_id and v.get("sample"):
            try:
                (config.voices_dir / v["sample"]).unlink(missing_ok=True)
            except OSError:
                pass
    reg["voices"] = kept
    _save(config, reg)
    return True
