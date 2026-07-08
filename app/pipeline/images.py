from __future__ import annotations

from pathlib import Path

import httpx

from app.images.base import ImageProvider
from app.models import Draft, slugify


def download_original_images(urls, dest_dir, fetch=None) -> list[str]:
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    if fetch is None:
        def fetch(u):
            return httpx.get(u, timeout=20.0, follow_redirects=True).content
    saved: list[str] = []
    for i, url in enumerate(urls):
        try:
            data = fetch(url)
        except Exception:
            continue
        if not data:
            continue
        ext = ".jpg" if any(s in url.lower() for s in (".jpg", ".jpeg")) else ".png"
        p = dest_dir / f"img-{i}{ext}"
        p.write_bytes(data)
        saved.append(p.name)
    return saved


def attach_cover(draft: Draft, provider: ImageProvider, images_dir) -> Draft:
    images_dir = Path(images_dir)
    # Don't overwrite a valid cover (e.g. one already set from body image)
    if draft.cover_image and (images_dir / draft.cover_image).exists():
        # Check it's not a mock placeholder
        try:
            if (images_dir / draft.cover_image).stat().st_size > 10_000:
                return draft  # valid cover already present
        except OSError:
            pass
    title = draft.title_candidates[0] if draft.title_candidates else "cover"
    name = f"cover-{slugify(title)[:40] or 'cover'}.png"
    out = images_dir / name
    try:
        provider.generate(prompt=f"科技自媒体封面图：{title}", out_path=out)
        draft.cover_image = name
    except Exception:
        draft.cover_image = None
    return draft


def inject_image_prompt(draft: Draft, prompt: str | None = None) -> str:
    """Return a placeholder prompt text for the article body.
    
    When no real image provider is configured, this prompt is appended to the
    draft body so users can copy it into an external image generation tool.
    """
    title = (draft.title_candidates[0] if draft.title_candidates
             else "cover")
    p = prompt or f"科技自媒体封面图：{title}"
    return f"\n\n:::image-prompt\n{p}\n:::\n"
