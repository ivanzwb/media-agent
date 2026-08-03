"""One-off repair for drafts that kept their [[IMG:N]] markers.

Drafts written before the markers resolved reliably still carry literal
"[[IMG:0]]" text in the body. This walks the saved drafts, works out which
picture each marker was pointing at, downloads the ones that are still
reachable, and clears the markers that cannot be resolved.

The mapping is a reconstruction, not a record: nothing stored on the draft
says which image a marker meant, so it is re-derived from the sources the
draft lists, in the order they were given to the writer. Run without
``apply`` first and read the mapping before letting it write.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.config import Config
from app.pipeline.localize import localize_reference_images
from app.pipeline.synthesizer import (
    _IMG_REF_RE, _MAX_REF_IMAGES, resolve_image_refs)

logger = logging.getLogger(__name__)

# An article's image list is whatever the page carried, which on news sites
# means QR codes, app badges and share buttons sit alongside the photographs.
# Putting one of those back into the body is worse than leaving a gap.
_FURNITURE = (
    "qrcode", "qr-code", "qr_code", "ewm", "erweima", "zxcode",
    "logo", "favicon", "icon", "avatar", "sprite", "spacer",
    "placeholder", "watermark", "share", "weixin", "wechat",
    "app-download", "appdownload", "download-app", "button",
)


def _is_furniture(url: str) -> bool:
    # The path only: query strings carry whole other URLs on image CDNs.
    path = urlsplit(url).path.lower()
    return any(word in path for word in _FURNITURE)


@dataclass
class DraftRepair:
    draft_id: int
    title: str
    resolved: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    applied: bool = False

    @property
    def markers(self) -> int:
        return len(self.resolved) + len(self.dropped) + len(self.skipped)


def _manifest(image_lists: list[list[str]]) -> list[str]:
    """The image order the writer was shown, one source after another.

    Images already localized keep their slot: a marker's number was fixed
    when the draft was written, and localizing the archive afterwards must
    not shift what it points at.
    """
    manifest: list[str] = []
    for images in image_lists:
        manifest.extend(list(images or [])[:_MAX_REF_IMAGES])
    return manifest


def _source_images(store, row, meta: dict) -> list[list[str]]:
    ids = [s.get("article_id") for s in (meta.get("sources") or [])
           if isinstance(s, dict) and s.get("article_id")]
    if not ids and row["article_id"]:
        ids = [row["article_id"]]
    out: list[list[str]] = []
    for article_id in ids:
        body = store.read_article_body(int(article_id))
        out.append(list(body.get("images") or []))
    return out


def _remote_refs(body_md: str, manifest: list[str]) -> list[str]:
    urls: list[str] = []
    for m in _IMG_REF_RE.finditer(body_md):
        n = int(m.group(1))
        if not (0 <= n < len(manifest)):
            continue
        url = manifest[n]
        if url.startswith(("http://", "https://")) and url not in urls:
            urls.append(url)
    return urls


def repair_draft_images(store, config: Config, *, apply: bool = False,
                        progress=None) -> list[DraftRepair]:
    """Find drafts still carrying [[IMG:N]] and resolve or clear the markers.

    Without *apply* nothing is written: the returned plan says what each
    marker would become.
    """
    emit = progress or (lambda _m: None)
    repairs: list[DraftRepair] = []
    for row in store.list_drafts():
        meta = store.read_draft_body(row["id"])
        body_md = meta.get("body_md") or ""
        if "[[IMG:" not in body_md:
            continue
        title = (meta.get("title_cn")
                 or (meta.get("title_candidates") or [""])[0]
                 or f"草稿 {row['id']}")
        emit(f"草稿 {row['id']}《{title[:30]}》：发现残留记号")

        found = _manifest(_source_images(store, row, meta))
        skipped = {m.group(0): found[int(m.group(1))]
                   for m in _IMG_REF_RE.finditer(body_md)
                   if 0 <= int(m.group(1)) < len(found)
                   and _is_furniture(found[int(m.group(1))])}
        manifest = ["" if _is_furniture(u) else u for u in found]

        local_map: dict[str, str] = {}
        refs = _remote_refs(body_md, manifest)
        if refs and apply:
            local_map = localize_reference_images(
                refs, config, progress=lambda m: emit(f"  {m}"))
        new_body, resolved, dropped = resolve_image_refs(
            body_md, manifest, local_map if apply else
            # A dry run cannot know whether a download will succeed, so it
            # reports the remote URL a marker points at as resolvable.
            {u: u for u in refs})
        repair = DraftRepair(
            row["id"], title, resolved,
            [token for token in dropped if token not in skipped],
            [f"{token} → {url}" for token, url in skipped.items()])

        if apply and new_body != body_md:
            store.update_draft_body(
                row["id"], list(meta.get("title_candidates") or [title]),
                new_body)
            repair.applied = True
            emit(f"  已修复：补回 {len(resolved)} 张，"
                 f"跳过版面装饰 {len(repair.skipped)} 处，"
                 f"清除 {len(repair.dropped)} 处")
        repairs.append(repair)
    return repairs
