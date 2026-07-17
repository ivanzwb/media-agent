from __future__ import annotations

import base64
import logging
from pathlib import Path

from openai import OpenAI

logger = logging.getLogger(__name__)


class OpenAIImageProvider:
    def __init__(self, api_key: str | None = None, model: str | None = None,
                 base_url: str | None = None):
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)
        self.model = model or "dall-e-3"

    def generate(self, prompt: str, out_path: Path) -> Path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Some OpenAI-compatible endpoints / litellm proxies and non-DALL·E
        # models reject optional params (response_format, size). Start with the
        # full request and drop whichever param the API complains about, then
        # retry — so a picky model still produces an image.
        base = {"model": self.model, "prompt": prompt, "n": 1}
        optional = {"size": "1024x1024", "response_format": "b64_json"}
        attempt = {**base, **optional}
        last_exc: Exception | None = None
        for _ in range(len(optional) + 1):
            try:
                resp = self.client.images.generate(**attempt)
                return self._save(resp, out_path)
            except Exception as exc:  # noqa: BLE001 - inspect + selectively retry
                msg = str(exc).lower()
                dropped = False
                for param in list(optional):
                    if (param in attempt and param.lower() in msg
                            and any(k in msg for k in (
                                "support", "unsupported", "drop_params",
                                "unexpected", "invalid", "unknown"))):
                        attempt.pop(param, None)
                        logger.warning(
                            "image API rejected '%s'; retrying without it (%s)",
                            param, self.model)
                        dropped = True
                last_exc = exc
                if not dropped:
                    raise
        if last_exc:
            raise last_exc
        raise RuntimeError("image generation failed")

    @staticmethod
    def _save(resp, out_path: Path) -> Path:
        item = resp.data[0]
        b64 = getattr(item, "b64_json", None)
        if b64:
            out_path.write_bytes(base64.b64decode(b64))
            return out_path
        url = getattr(item, "url", None)
        if url:
            import httpx
            r = httpx.get(url, timeout=60.0, follow_redirects=True)
            r.raise_for_status()
            out_path.write_bytes(r.content)
            return out_path
        raise RuntimeError("图像接口未返回 b64_json 或 url")
