from __future__ import annotations

import base64
from pathlib import Path

from openai import OpenAI


class OpenAIImageProvider:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.client = OpenAI(api_key=api_key)
        self.model = model or "dall-e-3"

    def generate(self, prompt: str, out_path: Path) -> Path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        resp = self.client.images.generate(
            model=self.model, prompt=prompt, size="1024x1024",
            response_format="b64_json", n=1)
        data = base64.b64decode(resp.data[0].b64_json)
        out_path.write_bytes(data)
        return out_path
