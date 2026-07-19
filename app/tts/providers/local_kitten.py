from __future__ import annotations

import asyncio
import re
from pathlib import Path

# In-process port of the Kitten TTS service. Chinese (and mixed) text is
# synthesized via Microsoft edge-tts (free, no API key, high quality) — the
# same engine the original service uses for Chinese. Runs inside this process,
# so no separate TTS server needs to be started.

# profile -> (Chinese voice, English voice)
_PROFILE_VOICES: dict[str, tuple[str, str]] = {
    "child":       ("zh-CN-XiaoxuanNeural", "en-US-AriaNeural"),
    "female":      ("zh-CN-XiaoxiaoNeural", "en-US-JennyNeural"),
    "female_warm": ("zh-CN-XiaoyiNeural",   "en-US-JennyNeural"),
    "male":        ("zh-CN-YunjianNeural",  "en-US-GuyNeural"),
    "male_deep":   ("zh-CN-YunyangNeural",  "en-US-DavisNeural"),
    "assistant":   ("zh-CN-XiaoxiaoNeural", "en-US-JennyNeural"),
}

# Friendly aliases (incl. Chinese) -> profile key.
_ALIASES: dict[str, str] = {
    "儿童": "child", "孩子": "child", "kid": "child",
    "女声": "female", "woman": "female", "girl": "female",
    "温柔女声": "female_warm", "warm_female": "female_warm",
    "男声": "male", "man": "male", "boy": "male",
    "低沉男声": "male_deep", "deep_male": "male_deep",
    "默认": "assistant", "助手": "assistant", "default": "assistant",
}

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")


class LocalKittenTTS:
    def __init__(self, voice: str | None = None, speed: float = 1.0,
                 rate: str | None = None, pitch: str | None = None,
                 volume: str | None = None):
        self.voice = (voice or "").strip()
        self.speed = speed
        # edge-tts prosody: rate "+10%"/"-10%", pitch "+15Hz"/"-10Hz"
        # (higher pitch + moderate rate reads livelier / more human).
        self.rate = (rate or "").strip()
        self.pitch = (pitch or "").strip()
        self.volume = (volume or "").strip()

    def _resolve_voice(self, chinese: bool) -> str:
        v = self.voice
        if v and "Neural" in v:  # already a concrete edge-tts voice
            return v
        key = (v or "assistant").lower()
        key = _ALIASES.get(key, key)
        zh, en = _PROFILE_VOICES.get(key, _PROFILE_VOICES["assistant"])
        return zh if chinese else en

    def synthesize(self, text: str, out_stem: Path) -> Path:
        import edge_tts  # imported lazily so construction never fails

        chinese = bool(_CJK_RE.search(text))
        voice = self._resolve_voice(chinese)
        # Explicit rate wins; else derive from speed. edge-tts wants "+N%".
        rate = self.rate or f"{int(round((self.speed - 1.0) * 100)):+d}%"
        pitch = self.pitch or "+0Hz"
        opts: dict = {"rate": rate, "pitch": pitch}
        if self.volume:
            opts["volume"] = self.volume
        path = Path(out_stem).with_suffix(".mp3")
        path.parent.mkdir(parents=True, exist_ok=True)

        async def _run() -> bytes:
            comm = edge_tts.Communicate(text, voice=voice, **opts)
            chunks: list[bytes] = []
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])
            return b"".join(chunks)

        data = asyncio.run(_run())
        if not data:
            raise RuntimeError("edge-tts 返回空音频（检查网络/文本）")
        path.write_bytes(data)
        return path
