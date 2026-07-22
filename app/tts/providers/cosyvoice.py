"""Compatibility import for the managed CosyVoice provider.

CosyVoice dependencies are intentionally not imported into the application
process. New code should import :mod:`cosyvoice_runtime` directly.
"""
from app.tts.providers.cosyvoice_runtime import CosyVoiceRuntimeTTS

CosyVoiceTTS = CosyVoiceRuntimeTTS

__all__ = ["CosyVoiceRuntimeTTS", "CosyVoiceTTS"]
