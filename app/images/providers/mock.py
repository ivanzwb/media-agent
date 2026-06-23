from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


class MockImageProvider:
    def generate(self, prompt: str, out_path: Path) -> Path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (1024, 576), (32, 80, 160))
        draw = ImageDraw.Draw(img)
        draw.text((40, 40), prompt[:60], fill=(255, 255, 255))
        img.save(out_path, format="PNG")
        return out_path
