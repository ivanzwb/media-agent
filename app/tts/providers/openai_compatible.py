from __future__ import annotations

from pathlib import Path

import httpx


class OpenAICompatibleTTS:
    """TTS over an OpenAI-compatible endpoint (POST {base}/audio/speech).

    Works with OpenAI's API and most local servers that expose the same shape
    (e.g. openedai-speech, kokoro-fastapi, GPT-SoVITS OpenAI mode, etc.).

    Configure `base_url` to your server's API root, e.g.
    `http://127.0.0.1:8000/v1`. The request body is:
        {"model": ..., "input": <text>, "voice": ..., "response_format": "mp3"}
    and the raw audio bytes are written to disk.
    """

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, voice: str | None = None,
                 response_format: str = "mp3", timeout: float = 120.0):
        self.base_url = (base_url or "http://127.0.0.1:8000/v1").rstrip("/")
        self.api_key = api_key
        self.model = model or "tts-1"
        self.voice = voice or "alloy"
        self.response_format = response_format
        self.timeout = timeout

    def synthesize(self, text: str, out_stem: Path) -> Path:
        path = Path(out_stem).with_suffix("." + self.response_format)
        path.parent.mkdir(parents=True, exist_ok=True)
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "response_format": self.response_format,
        }
        resp = httpx.post(f"{self.base_url}/audio/speech", json=payload,
                          headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        path.write_bytes(resp.content)
        return path
