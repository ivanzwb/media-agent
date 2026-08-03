"""One-off repair for the picture problems in drafts already written.

Two of them, both from the era before image markers resolved reliably:

* Drafts still carrying literal "[[IMG:0]]" text. Which picture each marker
  meant is a reconstruction, not a record — nothing on the draft says — so it
  is re-derived from the sources the draft lists, in the order they were given
  to the writer. Run without ``apply`` first and read the mapping.
* Drafts whose markers did resolve, into a comment icon or a site logo. Those
  are found by measuring the file, so no guessing is involved.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.config import Config
from app.pipeline.localize import (
    is_content_image, is_page_furniture, local_media_file,
    localize_reference_images)
from app.pipeline.synthesizer import (
    _IMG_REF_RE, _MAX_REF_IMAGES, resolve_image_refs)

logger = logging.getLogger(__name__)

_LOCAL_IMG_RE = re.compile(r"!\[[^\]]*\]\(((?:\.\./)*/?media/[^)\s]+)\)")


@dataclass
class DraftRepair:
    draft_id: int
    title: str
    resolved: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    icons: list[str] = field(default_factory=list)
    applied: bool = False

    @property
    def markers(self) -> int:
        return len(self.resolved) + len(self.dropped) + len(self.skipped)


def strip_icon_images(body_md: str, config: Config) -> tuple[str, list[str]]:
    """Remove body images whose file is too small to be a real picture."""
    removed: list[str] = []

    def replace(match: re.Match[str]) -> str:
        url = match.group(1)
        path = local_media_file(re.sub(r"^(\.\./)+", "/", url), config)
        if path is None or is_content_image(path):
            return match.group(0)
        removed.append(url)
        return ""

    body = _LOCAL_IMG_RE.sub(replace, body_md)
    return re.sub(r"\n{3,}", "\n\n", body), removed


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
    """Resolve or clear leftover [[IMG:N]] markers, and drop embedded icons.

    Without *apply* nothing is written: the returned plan says what each
    marker would become and which pictures would go.
    """
    emit = progress or (lambda _m: None)
    repairs: list[DraftRepair] = []
    for row in store.list_drafts():
        meta = store.read_draft_body(row["id"])
        body_md = meta.get("body_md") or ""
        has_markers = "[[IMG:" in body_md
        if not has_markers and not _LOCAL_IMG_RE.search(body_md):
            continue
        title = (meta.get("title_cn")
                 or (meta.get("title_candidates") or [""])[0]
                 or f"草稿 {row['id']}")

        new_body, resolved, dropped, skipped = body_md, [], [], {}
        if has_markers:
            emit(f"草稿 {row['id']}《{title[:30]}》：发现残留记号")
            found = _manifest(_source_images(store, row, meta))
            skipped = {m.group(0): found[int(m.group(1))]
                       for m in _IMG_REF_RE.finditer(body_md)
                       if 0 <= int(m.group(1)) < len(found)
                       and is_page_furniture(found[int(m.group(1))])}
            manifest = ["" if is_page_furniture(u) else u for u in found]

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

        new_body, icons = strip_icon_images(new_body, config)
        if icons and not has_markers:
            emit(f"草稿 {row['id']}《{title[:30]}》：正文里有 {len(icons)} 张图标")
        repair = DraftRepair(
            row["id"], title, resolved,
            [token for token in dropped if token not in skipped],
            [f"{token} → {url}" for token, url in skipped.items()],
            icons)

        if apply and new_body != body_md:
            store.update_draft_body(
                row["id"], list(meta.get("title_candidates") or [title]),
                new_body)
            repair.applied = True
            emit(f"  已修复：补回 {len(resolved)} 张，"
                 f"跳过版面装饰 {len(repair.skipped)} 处，"
                 f"清除记号 {len(repair.dropped)} 处，"
                 f"移除图标 {len(icons)} 张")
        repairs.append(repair)
    return repairs
