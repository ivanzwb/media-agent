from __future__ import annotations

import wave
from pathlib import Path

_FRAMERATE = 16000


class MockTTSProvider:
    """Offline placeholder: writes a silent WAV whose length scales with the
    text, so the rest of the pipeline (durations, file handling) works without
    a real TTS backend."""

    def synthesize(self, text: str, out_stem: Path) -> Path:
        path = Path(out_stem).with_suffix(".wav")
        path.parent.mkdir(parents=True, exist_ok=True)
        seconds = max(1.0, len((text or "").strip()) * 0.25)
        nframes = int(seconds * _FRAMERATE)
        with wave.open(str(path), "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(_FRAMERATE)
            w.writeframes(b"\x00\x00" * nframes)
        return path
