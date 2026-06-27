from __future__ import annotations

from pathlib import Path

# Fish Audio cloud TTS + voice cloning SDK.
# Pure HTTP client — works with any Python version (3.9+), no native deps.
# Install: pip install fish-audio-sdk
# Pricing: $15 / million UTF-8 bytes (S2 Pro model).
#
# Voice cloning modes:
#   1. Instant cloning — read a reference sample from disk and pass it as
#      ReferenceAudio.  Works without a pre‑registered voice model.
#   2. Persistent voice model — create once via client.voices.create() then
#      reference by ID.  (Not yet exposed; future enhancement.)
#
# Falls back to Fish Audio's built-in voice when no sample is available.


class FishAudioTTS:
    """TTS via Fish Audio cloud API (fish.audio).

    Requires ``FISH_API_KEY`` env var or ``api_key`` constructor arg.

    ``voice`` = local voice‑registry ID (same as xtts uses).  The provider
    resolves it to a reference sample path, reads the audio bytes, and uses
    instant voice cloning.  When ``voice`` is empty/missing the API uses its
    default voice.
    """

    def __init__(self, api_key: str | None = None,
                 speaker_wav: str | None = None,
                 model: str | None = None,
                 response_format: str = "mp3"):
        self.api_key = api_key
        self.speaker_wav = speaker_wav  # resolved reference audio path
        self.model = model or "s2-pro"
        self.response_format = response_format

    def synthesize(self, text: str, out_stem: Path) -> Path:
        try:
            from fishaudio import FishAudio
            from fishaudio.types import ReferenceAudio
        except ImportError as e:
            raise RuntimeError(
                "未安装 Fish Audio SDK：请 `pip install fish-audio-sdk`"
            ) from e

        path = Path(out_stem).with_suffix("." + self.response_format)
        path.parent.mkdir(parents=True, exist_ok=True)

        client = FishAudio(api_key=self.api_key)

        kwargs: dict = {
            "text": text,
            "format": self.response_format,
        }

        # Instant voice cloning via reference sample (if available).
        sample_path = self.speaker_wav
        if sample_path and Path(sample_path).exists():
            try:
                audio_bytes = Path(sample_path).read_bytes()
                # The reference text helps the model align — pass empty as
                # best-effort when we don't have the actual transcript.
                kwargs["references"] = [
                    ReferenceAudio(audio=audio_bytes, text="")]
            except OSError:
                pass  # fall through to default voice

        data: bytes = client.tts.convert(**kwargs)
        path.write_bytes(data)
        return path
