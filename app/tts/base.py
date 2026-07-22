from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Protocol


class TTSProvider(Protocol):
    def synthesize(self, text: str, out_stem: Path) -> Path:
        """Render `text` to an audio file based at `out_stem` (no suffix).

        Returns the actual written path (the provider chooses the extension).
        """
        ...


class AdjustableTTS:
    """Wrapper that applies ffmpeg post-processing for rate/pitch adjustment.

    Works with any TTS provider — rate uses atempo filter, pitch uses
    asetrate + aresample.  Both are optional; only the provider's raw audio
    is modified when parameters are provided.
    """

    def __init__(self, inner: TTSProvider, rate: str | None = None,
                 pitch: str | None = None):
        self._inner = inner
        # rate: "+10%" / "-10%" → atempo factor (1.1 / 0.9)
        self._atempo = _parse_rate(rate) if rate else None
        # pitch: "+15Hz" / "-10Hz" → semitone shift
        self._semitones = _parse_pitch(pitch) if pitch else None

    def synthesize(self, text: str, out_stem: Path) -> Path:
        raw_path = self._inner.synthesize(text, out_stem)
        if not self._atempo and not self._semitones:
            return raw_path
        return _ffmpeg_adjust(raw_path, self._atempo, self._semitones)


def _parse_rate(rate: str) -> float:
    """Convert edge-tts style rate (e.g. '+10%', '-10%') to atempo factor."""
    rate = rate.strip()
    if rate.endswith("%"):
        return 1.0 + int(rate.rstrip("%")) / 100.0
    return float(rate)  # already a factor like 1.2


def _parse_pitch(pitch: str) -> float:
    """Convert edge-tts style pitch (e.g. '+15Hz', '-10Hz') to semitones."""
    pitch = pitch.strip()
    if pitch.lower().endswith("hz"):
        hz = float(pitch[:-2])
        # Approximate: 1 semitone ≈ 5.95 Hz at 260 Hz (middle C)
        return hz / 5.95
    return float(pitch)  # already semitones


def _ffmpeg_adjust(src: Path, atempo: float | None = None,
                   semitones: float | None = None) -> Path:
    """Apply rate and/or pitch adjustment via ffmpeg, return output path."""
    filters: list[str] = []
    if atempo:
        # atempo only accepts 0.5–100.0; chain for extreme values
        factors = _chain_atempo(atempo)
        filters.append(f"atempo={factors}")
    if semitones:
        # asetrate shifts pitch without changing duration when combined
        # with aresample to restore original sample rate
        base_sr = 16000
        shifted_sr = int(base_sr * (2 ** (semitones / 12.0)))
        filters.append(f"asetrate={shifted_sr},aresample={base_sr}")
    if not filters:
        return src

    suffix = src.suffix or ".wav"
    out = src.with_suffix(f".adj{suffix}")
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-af", ",".join(filters),
        str(out),
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return out


def _chain_atempo(factor: float) -> str:
    """Chain atempo filters for factors outside 0.5–100.0."""
    if 0.5 <= factor <= 100.0:
        return f"{factor:.4g}"
    # For extreme values, chain multiple filters
    parts: list[str] = []
    remaining = factor
    while remaining > 100.0:
        parts.append("100.0")
        remaining /= 100.0
    while remaining < 0.5:
        parts.append("0.5")
        remaining /= 0.5
    parts.append(f"{remaining:.4g}")
    return ",".join(parts)


def get_tts_provider(name: str | None, base_url: str | None = None,
                     api_key: str | None = None, model: str | None = None,
                     voice: str | None = None, rate: str | None = None,
                     pitch: str | None = None,
                     instruct: str | None = None,
                     data_dir: Path | None = None) -> TTSProvider:
    # Default: in-process Kitten (edge-tts) — no external service needed.
    if name in (None, "", "kitten", "local"):
        from app.tts.providers.local_kitten import LocalKittenTTS
        inner = LocalKittenTTS(voice=voice)
    elif name == "mock":
        from app.tts.providers.mock import MockTTSProvider
        inner = MockTTSProvider()
    elif name in ("openai", "http", "openai_compatible"):
        from app.tts.providers.openai_compatible import OpenAICompatibleTTS
        inner = OpenAICompatibleTTS(base_url=base_url, api_key=api_key,
                                    model=model, voice=voice)
    elif name == "cosyvoice":
        # Heavy dependencies remain isolated in a managed subprocess runtime.
        # ``model`` is intentionally ignored: users never configure a source,
        # Python, or checkpoint path.
        del model
        if data_dir is None:
            raise RuntimeError("CosyVoice provider requires the managed data_dir")
        from app.tts.providers.cosyvoice_runtime import CosyVoiceRuntimeTTS
        inner = CosyVoiceRuntimeTTS(
            data_dir=data_dir, speaker_wav=voice, instruct=instruct)
    else:
        raise ValueError(f"Unknown TTS provider: {name}")

    # Wrap with ffmpeg post-processing when rate/pitch are specified
    if rate or pitch:
        return AdjustableTTS(inner, rate=rate, pitch=pitch)
    return inner
