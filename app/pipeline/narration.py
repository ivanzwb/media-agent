from __future__ import annotations

import json
import re
import secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
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
                       max_scenes: int = 12, progress=None,
                       tts_provider: str | None = None,
                       tts_voice: str | None = None) -> dict:
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

    # Add intro scene at position 0 and outro scene at end,
    # pre-filled with brand name and article title.
    brand = (config.video_brand_name or "").strip() or "Media Agent"
    scenes.insert(0, {
        "type": "intro",
        "narration": f"欢迎收看 {brand}，本期话题：{title}" if title else f"欢迎收看 {brand}",
        "visual": "片头画面：品牌名 + 文章标题",
        "media": "none",
    })
    scenes.append({
        "type": "outro",
        "narration": f"感谢收看 {brand}，我们下期再见！",
        "visual": "片尾画面：感谢观看 + 品牌名",
        "media": "none",
    })

    from app.pipeline.sanitizer import load_words, sanitize
    sens_words = load_words(config)
    if sens_words:
        total = 0
        for sc in scenes:
            clean, hits = sanitize(sc.get("narration", ""), sens_words)
            if hits:
                sc["narration"] = clean
                total += sum(h["count"] for h in hits)
        if total:
            emit(f"敏感词过滤 {total} 处")

    out_dir = config.videos_dir / f"draft-{draft_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    emit(f"共 {len(scenes)} 个分镜，开始并行配音…")

    def synth_one(i_sc):
        i, sc = i_sc
        try:
            audio = tts.synthesize(sc["narration"], out_dir / f"scene-{i}")
            return i, audio.name, None
        except Exception as e:  # noqa: BLE001
            return i, None, str(e)

    with ThreadPoolExecutor(max_workers=min(len(scenes), 6)) as pool:
        for i, fname, err in pool.map(synth_one, enumerate(scenes)):
            if err:
                scenes[i]["audio"] = None
                scenes[i]["audio_error"] = err
            else:
                scenes[i]["audio"] = fname

    script["draft_id"] = draft_id
    if tts_provider is not None:
        script["tts_provider"] = tts_provider
    if tts_voice is not None:
        script["tts_voice"] = tts_voice
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
            "type": sc.get("type"),  # preserve intro/outro/normal
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
                   config: Config, progress=None,
                   tts_provider: str | None = None,
                   tts_voice: str | None = None) -> dict:
    """Re-synthesize voiceover only for the given scene indices."""
    emit = progress or _noop
    script = load_narration(draft_id, config)
    if not script:
        raise ValueError("脚本不存在，请先生成讲解脚本")
    scenes = script.get("scenes", [])
    out_dir = config.videos_dir / f"draft-{draft_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    targets = [i for i in indices if 0 <= i < len(scenes)]
    emit(f"重新配音 {len(targets)} 个分镜（并行）…")

    def synth_one(i):
        sc = scenes[i]
        stem = out_dir / f"scene-{i}-{secrets.token_hex(3)}"
        try:
            audio = tts.synthesize(sc.get("narration", ""), stem)
            return i, audio.name, None
        except Exception as e:  # noqa: BLE001
            return i, None, str(e)

    with ThreadPoolExecutor(max_workers=min(len(targets), 6)) as pool:
        for i, fname, err in pool.map(synth_one, targets):
            if err:
                scenes[i]["audio"] = None
                scenes[i]["audio_error"] = err
            else:
                scenes[i]["audio"] = fname
                scenes[i].pop("audio_error", None)

    if tts_provider is not None:
        script["tts_provider"] = tts_provider
    if tts_voice is not None:
        script["tts_voice"] = tts_voice
    _save_script(draft_id, script, config)
    emit("配音更新完成")
    return script
