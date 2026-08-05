from __future__ import annotations

import json
import logging
import secrets
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.config import Config

logger = logging.getLogger(__name__)

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


def _convert_to_wav(src: Path, dst: Path) -> Path:
    """Transcode a voice sample to 16kHz mono WAV, raising on failure."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            f"声音样本是 {src.suffix or '未知'} 格式，语音引擎只认 WAV，"
            "转换需要 ffmpeg：请安装 ffmpeg 并加入 PATH（https://ffmpeg.org）")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-ar", "16000", "-ac", "1", str(dst)],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not dst.is_file():
        raise RuntimeError(
            f"声音样本 {src.name} 转 WAV 失败：{(proc.stderr or '')[-400:]}")
    logger.info("已缓存参考音频 %s", dst)
    return dst


def reference_wav(config: Config, voice_id: str | None) -> Path | None:
    """The voice sample in a format the TTS engines can actually open.

    A voice recorded in the browser arrives as WebM/Opus, which libsndfile
    (and so CosyVoice) cannot read at all — it fails with "Format not
    recognised". Anything that is not already WAV is transcoded once and
    cached next to the sample.
    """
    src = sample_path(config, voice_id)
    if src is None or src.suffix.lower() == ".wav":
        return src
    dst = src.with_name(f"{src.stem}_16k.wav")
    if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    return _convert_to_wav(src, dst)


def _ensure_16k_ref(config: Config, voice_id: str, filename: str) -> None:
    """Cache the WAV form of a fresh upload so the first synthesis is quick.

    Best-effort only: a failure here (no ffmpeg yet) surfaces with a proper
    message from :func:`reference_wav` when the voice is actually used.
    """
    ext = Path(filename or "").suffix.lower()
    if ext == ".wav":
        return
    try:
        _convert_to_wav(config.voices_dir / f"{voice_id}{ext}",
                        config.voices_dir / f"{voice_id}_16k.wav")
    except Exception as exc:  # noqa: BLE001 - retried lazily on first use
        logger.info("上传时转换参考音频失败（稍后重试）：%s", exc)


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
