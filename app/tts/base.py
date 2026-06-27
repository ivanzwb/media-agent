from __future__ import annotations

from pathlib import Path
from typing import Protocol


class TTSProvider(Protocol):
    def synthesize(self, text: str, out_stem: Path) -> Path:
        """Render `text` to an audio file based at `out_stem` (no suffix).

        Returns the actual written path (the provider chooses the extension).
        """
        ...


def get_tts_provider(name: str | None, base_url: str | None = None,
                     api_key: str | None = None, model: str | None = None,
                     voice: str | None = None) -> "TTSProvider":
    # Default: in-process Kitten (edge-tts) — no external service needed.
    if name in (None, "", "kitten", "local"):
        from app.tts.providers.local_kitten import LocalKittenTTS
        return LocalKittenTTS(voice=voice)
    if name == "mock":
        from app.tts.providers.mock import MockTTSProvider
        return MockTTSProvider()
    if name in ("openai", "http", "openai_compatible"):
        from app.tts.providers.openai_compatible import OpenAICompatibleTTS
        return OpenAICompatibleTTS(base_url=base_url, api_key=api_key,
                                   model=model, voice=voice)
    if name == "fishaudio":
        from app.tts.providers.fish_audio import FishAudioTTS
        return FishAudioTTS(api_key=api_key, speaker_wav=voice,
                            model=model)
    if name == "kitten_http":
        from app.tts.providers.kitten import KittenTTSProvider
        return KittenTTSProvider(base_url=base_url, voice=voice)
    if name == "cosyvoice":
        # voice = speaker reference wav path (resolved by caller);
        # model = CosyVoice2 model name/path (HuggingFace ID or local dir).
        from app.tts.providers.cosyvoice import CosyVoiceTTS
        return CosyVoiceTTS(speaker_wav=voice, model=model)
    raise ValueError(f"Unknown TTS provider: {name}")
