from __future__ import annotations

import json
from pathlib import Path

from app.config import Config
from app.llm.base import LLMProvider
from app.pipeline.script import build_script
from app.store import Store
from app.tts.base import TTSProvider


def _noop(*_a, **_k) -> None:
    pass


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
