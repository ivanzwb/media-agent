"""Digital-human (数字人主播) presenter for the explainer video.

Given a presenter portrait image and a scene's narration audio, produce a
talking-head clip and composite it onto the scene:

  - ``pip``  — small presenter in the bottom-right corner (picture-in-picture)
  - ``full`` — presenter fills the frame (news-anchor style), narration subtitle
               overlaid on top

The talking head itself is produced by a pluggable provider:

  - ``sadtalker`` — local lip-sync via a SadTalker checkout (GPU). Best-effort:
    if SadTalker isn't configured/available or fails, we fall back to a static
    presenter image (still shows the avatar, just without lip motion) so video
    synthesis never breaks.

All compositing is plain ffmpeg (matching builder.py), so the static path is
fully testable without any ML models installed.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

W, H = 1280, 720
_FPS = 30
_SAMPLE_RATE = 44100
_PIP_HEIGHT = int(H * 0.42)   # presenter ~302px tall in the corner
_PIP_MARGIN = 30


@dataclass
class AvatarSpec:
    """Resolved digital-human config for a video build."""
    enabled: bool = False
    image: Path | None = None            # presenter portrait (absolute path)
    provider: str = "sadtalker"          # sadtalker | still
    position: str = "pip"                # default placement: pip | full
    sadtalker_dir: str | None = None     # path to a SadTalker checkout
    sadtalker_python: str | None = None  # python exe for SadTalker (optional)
    checkpoint_dir: str | None = None    # managed model checkpoint directory

    def ready(self) -> bool:
        return bool(self.enabled and self.image and Path(self.image).exists())


def _run(cmd: list[str], timeout: float | None = None) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 失败：{' '.join(str(c) for c in cmd[:3])}…\n{proc.stderr[-800:]}")


def scene_avatar_mode(scene: dict, spec: AvatarSpec) -> str:
    """Return the presenter placement for a scene: ``off`` | ``pip`` | ``full``.

    A per-scene ``avatar`` field overrides the global default position; empty
    means "use the global default"; "off"/"none" disables it for that scene.
    """
    if not spec.ready():
        return "off"
    v = str(scene.get("avatar", "") or "").strip().lower()
    if v in ("off", "none", "no", "false", "0"):
        return "off"
    if v in ("pip", "full"):
        return v
    return spec.position if spec.position in ("pip", "full") else "pip"


# ---------------------------------------------------------------------------
# talking-head generation (SadTalker, best-effort)
# ---------------------------------------------------------------------------

def sadtalker_available(spec: AvatarSpec) -> bool:
    d, py, checkpoints = (
        spec.sadtalker_dir, spec.sadtalker_python, spec.checkpoint_dir)
    if not d or not py or not checkpoints:
        return False
    p = Path(d)
    cp = Path(checkpoints)
    return (
        p.is_dir()
        and (p / "inference.py").is_file()
        and Path(py).is_file()
        and (cp / "SadTalker_V0.0.2_256.safetensors").is_file()
        and (cp / "mapping_00109-model.pth.tar").is_file()
        and (cp / "mapping_00229-model.pth.tar").is_file()
    )


def _to_wav(audio: Path, out_wav: Path) -> Path | None:
    try:
        _run(["ffmpeg", "-y", "-i", str(audio), "-ar", "16000", "-ac", "1",
              str(out_wav)], timeout=120)
        return out_wav if out_wav.exists() else None
    except Exception as e:  # noqa: BLE001
        logger.warning("avatar: audio->wav 转换失败：%s", e)
        return None


def generate_talking_head(audio: Path, spec: AvatarSpec,
                          work_dir: Path, idx: int) -> Path | None:
    """Produce a lip-synced talking-head mp4 for *audio*, or None if the
    configured provider is unavailable/failed (caller falls back to still)."""
    if spec.provider != "sadtalker" or not sadtalker_available(spec):
        return None
    try:
        result_dir = work_dir / f"avatar-{idx}"
        result_dir.mkdir(parents=True, exist_ok=True)
        wav = _to_wav(audio, result_dir / "drive.wav") if audio else None
        if wav is None:
            return None
        py = str(spec.sadtalker_python)
        cmd = [py, "inference.py",
               "--driven_audio", str(wav),
               "--source_image", str(spec.image),
               "--checkpoint_dir", str(spec.checkpoint_dir),
               "--result_dir", str(result_dir),
               "--still", "--preprocess", "full"]
        from app.video.sadtalker_setup import runtime_target
        target = runtime_target()
        if target is not None and target.device == "cpu":
            cmd.extend(["--device", "cpu"])
        # The managed base install intentionally excludes the optional GFPGAN
        # weights. SadTalker's default (no enhancer flag) is reliable for both
        # PiP and full-frame modes and avoids a false "installed" state.
        logger.info("avatar: running SadTalker for scene #%d (enhancer=none) …",
                     idx + 1)
        proc = subprocess.run(
            cmd, cwd=spec.sadtalker_dir, capture_output=True, text=True,
            timeout=3600 if target is not None
            and getattr(target, "slow", False) else 900)
        if proc.returncode != 0:
            logger.warning("avatar: SadTalker 失败（回退到静态头像）：%s",
                           (proc.stderr or "")[-400:])
            return None
        mp4s = sorted(result_dir.rglob("*.mp4"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        return mp4s[0] if mp4s else None
    except Exception as e:  # noqa: BLE001 - never break the build
        logger.warning("avatar: SadTalker 异常（回退到静态头像）：%s", e)
        return None


# ---------------------------------------------------------------------------
# compositing
# ---------------------------------------------------------------------------

def _composite_pip(clip: Path, head_mp4: Path | None, image: Path,
                   narration: str, duration: float, work_dir: Path,
                   idx: int, out: Path) -> None:
    """Overlay the presenter in the bottom-right corner of *clip*, keeping the
    clip's own video timeline and narration audio. Redraw subtitles LAST so
    the presenter can never cover them."""
    from app.video.builder import render_subtitle_overlay  # lazy (avoid cycle)
    dur = f"{max(1.0, duration):.2f}"
    sub = render_subtitle_overlay(narration, work_dir / f"av-sub-{idx}.png")
    common = ["-map", "[v]", "-map", "0:a?", "-c:v", "libx264",
              "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
              "-ar", str(_SAMPLE_RATE), "-ac", "2", "-t", dur]
    ov = (f"[1:v]scale=-2:{_PIP_HEIGHT}[a];"
          f"[0:v][a]overlay=W-w-{_PIP_MARGIN}:H-h-{_PIP_MARGIN}:shortest=1[av];"
          f"[av][2:v]overlay=0:0[v]")
    if head_mp4 and head_mp4.exists():
        _run(["ffmpeg", "-y", "-i", str(clip), "-stream_loop", "-1",
              "-i", str(head_mp4), "-i", str(sub),
              "-filter_complex", ov, *common, str(out)])
    else:
        _run(["ffmpeg", "-y", "-i", str(clip), "-loop", "1",
              "-i", str(image), "-i", str(sub),
              "-filter_complex", ov, *common, str(out)])


def _composite_full(clip: Path, head_mp4: Path | None, image: Path,
                    narration: str, duration: float, work_dir: Path,
                    idx: int, out: Path) -> None:
    """Presenter fills the frame; the clip's narration audio is kept and the
    subtitle is overlaid on top of the presenter."""
    from app.video.builder import render_subtitle_overlay  # lazy (avoid cycle)
    dur = f"{max(1.0, duration):.2f}"
    sub = render_subtitle_overlay(narration, work_dir / f"av-sub-{idx}.png")
    fill = (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},setsar=1,fps={_FPS}")
    vf = f"[1:v]{fill}[bg];[bg][2:v]overlay=0:0[v]"
    common = ["-map", "[v]", "-map", "0:a?", "-c:v", "libx264",
              "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
              "-ar", str(_SAMPLE_RATE), "-ac", "2", "-t", dur]
    if head_mp4 and head_mp4.exists():
        _run(["ffmpeg", "-y", "-i", str(clip), "-stream_loop", "-1",
              "-i", str(head_mp4), "-i", str(sub),
              "-filter_complex", vf, *common, str(out)])
    else:
        _run(["ffmpeg", "-y", "-i", str(clip), "-loop", "1", "-i", str(image),
              "-i", str(sub), "-filter_complex", vf, *common, str(out)])


def composite_scene(clip: Path, audio: Path | None, duration: float,
                    narration: str, mode: str, spec: AvatarSpec,
                    work_dir: Path, idx: int, emit=None) -> None:
    """Composite the presenter onto *clip* in place (mode: pip|full)."""
    emit = emit or (lambda *_: None)
    if mode not in ("pip", "full") or not spec.ready():
        return
    head = generate_talking_head(audio, spec, work_dir, idx) if audio else None
    kind = "口型同步" if head else "静态头像"
    emit(f"  分镜 #{idx + 1}：数字人主播（{mode} · {kind}）")
    tmp = clip.with_suffix(".av.mp4")
    try:
        if mode == "pip":
            _composite_pip(clip, head, spec.image, narration, duration,
                           work_dir, idx, tmp)
        else:
            _composite_full(clip, head, spec.image, narration, duration,
                            work_dir, idx, tmp)
        os.replace(str(tmp), str(clip))
    except Exception as e:  # noqa: BLE001 - keep the plain clip on failure
        logger.warning("avatar: 合成失败，保留原分镜 #%d：%s", idx + 1, e)
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
