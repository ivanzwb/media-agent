from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ImageProvider(Protocol):
    def generate(self, prompt: str, out_path: Path) -> Path:
        ...


def get_image_provider(name: str | None, api_key: str | None = None,
                       model: str | None = None) -> "ImageProvider":
    if name in ("mock", None, ""):
        from app.images.providers.mock import MockImageProvider
        return MockImageProvider()
    if name == "openai":
        from app.images.providers.openai_images import OpenAIImageProvider
        return OpenAIImageProvider(api_key=api_key, model=model)
    raise ValueError(f"Unknown image provider: {name}")
