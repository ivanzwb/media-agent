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

_MEDIA_RE = re.compile(r"^(image|video):\d+$|^upload:.+$")


def _noop(*_a, **_k) -> None:
    pass


def _tts_workers(tts: TTSProvider) -> int:
    """How many scenes to voice in parallel.

    Network/cloud TTS (edge-tts / Kitten HTTP / OpenAI-compatible / Fish Audio)
    is latency-bound, so several concurrent requests are far faster. A local
    single-GPU model (CosyVoice) already uses the GPU per call and isn't safe to
    run concurrently on one device, so keep it serial.
    """
    name = type(tts).__name__.lower()
    if "cosyvoice" in name:
        return 1
    return 4


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

    workers = min(_tts_workers(tts), max(1, len(scenes)))

    def _synth(i: int, sc: dict) -> tuple[int, str | None, str | None]:
        try:
            audio = tts.synthesize(sc["narration"], out_dir / f"scene-{i}")
            return i, audio.name, None
        except Exception as e:  # noqa: BLE001 - surface per-scene failures
            return i, None, str(e)

    emit(f"共 {len(scenes)} 个分镜，开始配音（并发 {workers}）…")
    if workers <= 1:
        for i, sc in enumerate(scenes):
            emit(f"配音 {i + 1}/{len(scenes)}：{sc['narration'][:30]}")
            _, name, err = _synth(i, sc)
            sc["audio"] = name
            if err:
                sc["audio_error"] = err
                emit(f"  配音失败：{err}")
    else:
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_synth, i, sc) for i, sc in enumerate(scenes)]
            for f in as_completed(futs):
                i, name, err = f.result()
                scenes[i]["audio"] = name
                done += 1
                if err:
                    scenes[i]["audio_error"] = err
                    emit(f"  配音失败 #{i + 1}：{err}")
                else:
                    emit(f"配音 {done}/{len(scenes)} 完成")

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
        data = json.loads(p.read_text(encoding="utf-8"))
        if not data.get("scenes"):
            return None  # treat empty scenes as non-existent
        return data
    except (OSError, json.JSONDecodeError):
        return None


def _save_script(draft_id: int, script: dict, config: Config) -> None:
    out_dir = config.videos_dir / f"draft-{draft_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    script["draft_id"] = draft_id
    (out_dir / "script.json").write_text(
        json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")


def _extract_upload(media: str) -> str | None:
    """Extract upload filename from an 'upload:xxx' media value."""
    if media.startswith("upload:"):
        return media[len("upload:"):]
    return None


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
        if sc.get("bg_custom"):
            item["bg_custom"] = sc["bg_custom"]
        clean.append(item)
    script["scenes"] = clean
    # Collect all upload references into a flat list so the frontend
    # doesn't need to derive it from scenes (avoids disappearing uploads).
    uploads: list[str] = []
    for it in clean:
        p = it.get("bg_custom") or _extract_upload(it.get("media", ""))
        if p and p not in uploads:
            uploads.append(p)
    script["uploads"] = uploads
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
    workers = min(_tts_workers(tts), max(1, len(targets)))
    emit(f"重新配音 {len(targets)} 个分镜（并发 {workers}）…")

    def _resynth(i: int) -> tuple[int, str | None, str | None]:
        # unique name so reordered/edited scenes don't clobber each other
        stem = out_dir / f"scene-{i}-{secrets.token_hex(3)}"
        try:
            audio = tts.synthesize(scenes[i].get("narration", ""), stem)
            return i, audio.name, None
        except Exception as e:  # noqa: BLE001
            return i, None, str(e)

    if workers <= 1:
        for i in targets:
            emit(f"配音 #{i + 1}：{scenes[i].get('narration', '')[:30]}")
            _, name, err = _resynth(i)
            scenes[i]["audio"] = name
            if err:
                scenes[i]["audio_error"] = err
                emit(f"  失败：{err}")
            else:
                scenes[i].pop("audio_error", None)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_resynth, i) for i in targets]
            for f in as_completed(futs):
                i, name, err = f.result()
                scenes[i]["audio"] = name
                if err:
                    scenes[i]["audio_error"] = err
                    emit(f"  失败 #{i + 1}：{err}")
                else:
                    scenes[i].pop("audio_error", None)
                    emit(f"配音 #{i + 1} 完成")

    if tts_provider is not None:
        script["tts_provider"] = tts_provider
    if tts_voice is not None:
        script["tts_voice"] = tts_voice
    _save_script(draft_id, script, config)
    emit("配音更新完成")
    return script
