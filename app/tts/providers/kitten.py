from __future__ import annotations

from pathlib import Path

import httpx

# Built-in style profiles exposed by the Kitten TTS service.
_PROFILES = {
    "child", "female", "female_warm", "male", "male_deep", "assistant",
}


class KittenTTSProvider:
    """TTS over the local Kitten TTS service.

    Endpoint: POST {base}/v1/tts/kitten
    Body: {"text", "profile"|"voice", "speed", "format": "wav"}
    With format=wav the service returns raw WAV bytes, which we write to disk.

    `base_url` may be the service root (e.g. http://127.0.0.1:3003) or include
    a trailing /v1 — both are normalized. `voice` accepts either a style
    profile (child/female/male/...) or a concrete Kitten voice name.
    """

    def __init__(self, base_url: str | None = None, voice: str | None = None,
                 speed: float = 1.0, timeout: float = 120.0):
        root = (base_url or "http://127.0.0.1:3003").rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        self.base_url = root.rstrip("/")
        self.voice = (voice or "").strip()
        self.speed = speed
        self.timeout = timeout

    def synthesize(self, text: str, out_stem: Path) -> Path:
        path = Path(out_stem).with_suffix(".wav")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict = {"text": text, "speed": self.speed, "format": "wav"}
        if not self.voice:
            payload["profile"] = "female"
        elif self.voice in _PROFILES:
            payload["profile"] = self.voice
        else:
            payload["voice"] = self.voice
        resp = httpx.post(f"{self.base_url}/v1/tts/kitten", json=payload,
                          timeout=self.timeout)
        resp.raise_for_status()
        path.write_bytes(resp.content)
        return path
