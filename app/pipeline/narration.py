from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

from app.config import Config
from app.llm.base import LLMProvider
from app.pipeline.script import build_script
from app.store import Store
from app.tts.base import TTSProvider

_MEDIA_RE = re.compile(r"^(image|video):\d+$")


def _noop(*_a, **_k) -> None:
    pass


def _valid_media(m: str) -> str:
    m = str(m or "none").strip()
    return m if (m == "none" or _MEDIA_RE.match(m)) else "none"


def generate_narration(draft_id: int, store: Store, llm: LLMProvider,
                       tts: TTSProvider, config: Config,
                       max_scenes: int = 12, progress=None) -> dict:
    """Build a shot-by-shot narration script for a draft and synthesize a
    voiceover audio clip per scene. Returns the script (scenes annotated with
    their audio file names). Audio + script.json land in
    `data/videos/draft-<id>/`.
    """
    emit = progress or _noop
    meta = store.read_draft_body(draft_id)
    if not meta:
        raise ValueError("草稿不存在或无正文")
    body_md = meta.get("body_md", "")
    titles = meta.get("title_candidates") or []
    title = titles[0] if titles else ""

    emit("生成分镜脚本…")
    script = build_script(body_md, title, llm, max_scenes=max_scenes)
    scenes = script["scenes"]

    out_dir = config.videos_dir / f"draft-{draft_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    emit(f"共 {len(scenes)} 个分镜，开始逐段配音…")
    for i, sc in enumerate(scenes):
        emit(f"配音 {i + 1}/{len(scenes)}：{sc['narration'][:30]}")
        try:
            audio = tts.synthesize(sc["narration"], out_dir / f"scene-{i}")
            sc["audio"] = audio.name
        except Exception as e:  # noqa: BLE001 - surface per-scene failures
            sc["audio"] = None
            sc["audio_error"] = str(e)
            emit(f"  配音失败：{e}")

    script["draft_id"] = draft_id
    (out_dir / "script.json").write_text(
        json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
    emit("讲解脚本与配音完成")
    return script


def load_narration(draft_id: int, config: Config) -> dict | None:
    p = config.videos_dir / f"draft-{draft_id}" / "script.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_script(draft_id: int, script: dict, config: Config) -> None:
    out_dir = config.videos_dir / f"draft-{draft_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    script["draft_id"] = draft_id
    (out_dir / "script.json").write_text(
        json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")


def save_scenes(draft_id: int, scenes: list, config: Config) -> dict:
    """Persist edited scenes (narration/visual/media/audio) to script.json,
    keeping the existing images/videos asset lists."""
    script = load_narration(draft_id, config) or {
        "scenes": [], "images": [], "videos": []}
    clean: list[dict] = []
    for sc in scenes:
        narration = str((sc or {}).get("narration", "")).strip()
        if not narration:
            continue
        item = {
            "narration": narration,
            "visual": str(sc.get("visual", "")).strip(),
            "media": _valid_media(sc.get("media", "none")),
            "audio": sc.get("audio") or None,
        }
        if sc.get("audio_error"):
            item["audio_error"] = sc["audio_error"]
        clean.append(item)
    script["scenes"] = clean
    _save_script(draft_id, script, config)
    return script


def resynth_scenes(draft_id: int, indices: list[int], tts: TTSProvider,
                   config: Config, progress=None) -> dict:
    """Re-synthesize voiceover only for the given scene indices."""
    emit = progress or _noop
    script = load_narration(draft_id, config)
    if not script:
        raise ValueError("脚本不存在，请先生成讲解脚本")
    scenes = script.get("scenes", [])
    out_dir = config.videos_dir / f"draft-{draft_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    targets = [i for i in indices if 0 <= i < len(scenes)]
    emit(f"重新配音 {len(targets)} 个分镜…")
    for i in targets:
        sc = scenes[i]
        emit(f"配音 #{i + 1}：{sc.get('narration', '')[:30]}")
        try:
            # unique name so reordered/edited scenes don't clobber each other
            stem = out_dir / f"scene-{i}-{secrets.token_hex(3)}"
            audio = tts.synthesize(sc.get("narration", ""), stem)
            sc["audio"] = audio.name
            sc.pop("audio_error", None)
        except Exception as e:  # noqa: BLE001
            sc["audio"] = None
            sc["audio_error"] = str(e)
            emit(f"  失败：{e}")

    _save_script(draft_id, script, config)
    emit("配音更新完成")
    return script
