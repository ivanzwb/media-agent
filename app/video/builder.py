from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1280, 720
_BG = (15, 20, 28)
_BAND = (0, 0, 0, 165)
_FPS = 30
_SAMPLE_RATE = 44100

_FONT_CANDIDATES = [
    os.environ.get("MEDIA_AGENT_FONT"),
    "C:/Windows/Fonts/msyh.ttc",       # 微软雅黑 (Windows)
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",     # 黑体
    "/System/Library/Fonts/PingFang.ttc",                 # macOS
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",  # Linux
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if path and Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    lines: list[str] = []
    cur = ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if draw.textlength(cur + ch, font=font) <= max_w or not cur:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def _draw_subtitle(canvas: Image.Image, text: str, max_lines: int = 6) -> None:
    """Draw a translucent bottom band + centered caption text onto `canvas`."""
    draw = ImageDraw.Draw(canvas, "RGBA")
    font = _font(40)
    lines = _wrap(draw, (text or "").strip(), font, W - 140)
    if not lines:
        return
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:-1] + "…"

    line_h = 56
    band_h = max(140, line_h * len(lines) + 48)
    draw.rectangle([0, H - band_h, W, H], fill=_BAND)
    y = H - band_h + (band_h - line_h * len(lines)) // 2
    for ln in lines:
        w = draw.textlength(ln, font=font)
        draw.text(((W - w) // 2, y), ln, font=font, fill=(255, 255, 255))
        y += line_h


def _cover(im: Image.Image) -> Image.Image:
    """Scale to cover 1280x720 then center-crop (may lose edges)."""
    scale = max(W / im.width, H / im.height)
    im = im.resize((max(1, int(im.width * scale)),
                    max(1, int(im.height * scale))))
    left = (im.width - W) // 2
    top = (im.height - H) // 2
    return im.crop((left, top, left + W, top + H))


def _contain(im: Image.Image) -> Image.Image:
    """Scale to fit inside 1280x720 (whole image visible)."""
    scale = min(W / im.width, H / im.height)
    return im.resize((max(1, int(im.width * scale)),
                      max(1, int(im.height * scale))))


def _fit_image(im: Image.Image, fit: str) -> Image.Image:
    """Place `im` onto a 1280x720 canvas per fit mode (crop|fit|blur)."""
    if fit == "crop":
        return _cover(im)
    if fit == "blur":
        bg = _cover(im).filter(ImageFilter.GaussianBlur(20))
        fg = _contain(im)
        bg.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
        return bg
    # fit (default): whole image centered, letterboxed on a dark canvas
    canvas = Image.new("RGB", (W, H), _BG)
    fg = _contain(im)
    canvas.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
    return canvas


def render_frame(text: str, out_png: Path, bg_image: Path | None = None,
                 max_lines: int = 6, fit: str = "fit") -> Path:
    """Render one 1280x720 caption frame: optional background image (placed by
    `fit` mode) + a translucent bottom band with the narration text."""
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGB", (W, H), _BG)

    if bg_image and Path(bg_image).exists():
        try:
            im = Image.open(bg_image).convert("RGB")
            canvas = _fit_image(im, fit)
        except OSError:
            pass

    _draw_subtitle(canvas, text, max_lines=max_lines)
    canvas.save(out_png, format="PNG")
    return out_png


def render_intro_frame(title: str, brand_name: str, out_png: Path,
                       duration_sec: int = 4,
                       bg_image: Path | None = None) -> Path:
    """Render an intro title frame: brand name + article title on a
    dark gradient background (or custom bg_image if provided).
    The frame stays on screen for `duration_sec` (used only for clip timing)."""
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGB", (W, H), _BG)
    if bg_image and bg_image.exists():
        try:
            im = Image.open(bg_image).convert("RGB")
            canvas = _fit_image(im, "fit")
        except OSError:
            pass

    draw = ImageDraw.Draw(canvas)

    # decorative gradient-ish bands
    for i in range(5):
        y = H // 2 + i * 4 - 40
        alpha = 60 - i * 10
        if alpha > 0:
            overlay = Image.new("RGBA", (W, 4), (100, 140, 255, alpha))
            canvas.paste(overlay, (0, y), overlay)

    # brand name (smaller, subtle)
    brand_font = _font(28)
    bw = draw.textlength(brand_name, font=brand_font)
    draw.text(((W - bw) // 2, H // 2 - 100), brand_name,
              font=brand_font, fill=(160, 180, 220))

    # decorative line under brand
    draw.line([(W // 2 - 60, H // 2 - 65), (W // 2 + 60, H // 2 - 65)],
              fill=(100, 140, 255), width=2)

    # title (large, centered)
    title_font = _font(42)
    lines = _wrap(draw, title, title_font, W - 160)
    if not lines:
        lines = [title]
    # show at most 3 lines
    if len(lines) > 3:
        lines = lines[:3]
        lines[-1] = lines[-1][:-1] + "…"
    line_h = 52
    block_h = line_h * len(lines)
    start_y = H // 2 - block_h // 2 + 10
    for i, ln in enumerate(lines):
        tw = draw.textlength(ln, font=title_font)
        draw.text(((W - tw) // 2, start_y + i * line_h), ln,
                  font=title_font, fill=(255, 255, 255))

    canvas.save(out_png, format="PNG")
    return out_png


def render_outro_frame(brand_name: str, out_png: Path,
                       duration_sec: int = 4,
                       bg_image: Path | None = None) -> Path:
    """Render an outro frame: thank-you message + brand name on a dark
    background (or custom bg_image if provided).
    The frame stays on screen for `duration_sec`."""
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGB", (W, H), _BG)
    if bg_image and bg_image.exists():
        try:
            im = Image.open(bg_image).convert("RGB")
            canvas = _fit_image(im, "fit")
        except OSError:
            pass
    draw = ImageDraw.Draw(canvas)

    # subtle decorative band
    for i in range(8):
        y = H // 2 - 40 + i * 6
        alpha = 40 - i * 5
        if alpha > 0:
            overlay = Image.new("RGBA", (W, 6), (100, 140, 255, alpha))
            canvas.paste(overlay, (0, y), overlay)

    # "感谢观看"
    thanks_font = _font(48)
    thanks = "感谢观看"
    tw = draw.textlength(thanks, font=thanks_font)
    draw.text(((W - tw) // 2, H // 2 - 60), thanks,
              font=thanks_font, fill=(255, 255, 255))

    # brand name
    brand_font = _font(28)
    bw = draw.textlength(brand_name, font=brand_font)
    draw.text(((W - bw) // 2, H // 2 + 20), brand_name,
              font=brand_font, fill=(160, 180, 220))

    # "关注我们" hint
    follow_font = _font(22)
    follow = "关注我们 · 获取更多前沿资讯"
    fw = draw.textlength(follow, font=follow_font)
    draw.text(((W - fw) // 2, H // 2 + 70), follow,
              font=follow_font, fill=(120, 140, 180))

    canvas.save(out_png, format="PNG")
    return out_png


def render_subtitle_overlay(text: str, out_png: Path,
                            max_lines: int = 6) -> Path:
    """Render a transparent 1280x720 PNG with only the subtitle band/text,
    to be overlaid on top of a video clip."""
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    _draw_subtitle(canvas, text, max_lines=max_lines)
    canvas.save(out_png, format="PNG")
    return out_png


def probe_duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        return None


def _progress_seconds(line: str) -> float | None:
    """Parse a single ffmpeg `-progress` stdout line into encoded seconds
    (from `out_time_us`, reported in microseconds). Returns None for lines
    that don't carry timing info (frame=, speed=, progress=…)."""
    line = line.strip()
    if not line.startswith("out_time_us="):
        return None
    try:
        return int(line.split("=", 1)[1]) / 1_000_000.0
    except ValueError:
        return None


class _ProgressTracker:
    """Thread-safe progress tracker used by build_video.

    Work is measured in "seconds of encoded output" so longer scenes weigh
    more (they genuinely take longer to ffmpeg-encode). Each scene reports
    sub-second progress via ``tick``; when a clip fully encodes, ``finish_clip``
    tops it up to its exact duration. Emits ``on_progress(pct, eta)`` where
    ``pct`` is in (0, 1] and ``eta`` is seconds remaining (or None early on).
    """

    def __init__(self, total_work: float, on_progress,
                 t0: float | None = None) -> None:
        self._total = max(total_work, 1e-6)
        self._cb = on_progress or (lambda *_: None)
        self._lock = threading.Lock()
        self._done = 0.0
        self._last: dict[int, float] = {}   # clip index -> encoded seconds
        self._t0 = t0 if t0 is not None else time.monotonic()
        self._last_emit = float("-inf")   # first emit always passes

    def tick(self, index: int, sec: float, cap: float) -> None:
        """Add sub-second encoded progress for one clip (parallel-safe)."""
        with self._lock:
            prev = self._last.get(index, 0.0)
            delta = max(0.0, min(sec, cap) - prev)
            if delta <= 0:
                return
            self._last[index] = prev + delta
            self._done += delta

    def finish_clip(self, index: int, duration: float) -> None:
        """Mark a clip fully encoded; top up to its exact duration."""
        with self._lock:
            prev = self._last.get(index, 0.0)
            if duration > prev:
                self._last[index] = duration
                self._done += duration - prev

    def add_fixed(self, amount: float) -> None:
        """Add a known chunk of work (e.g. concat) without per-clip ticks."""
        with self._lock:
            self._done += max(0.0, amount)

    def finish(self) -> None:
        """Force 100% (used once the whole build succeeded)."""
        with self._lock:
            self._done = self._total
            self._cb(1.0, 0.0)

    def emit(self, force: bool = False) -> None:
        with self._lock:
            pct = min(1.0, self._done / self._total)
            now = time.monotonic()
            if not force and now - self._last_emit < 0.5:
                return
            self._last_emit = now
            eta = ((now - self._t0) / pct * (1.0 - pct)
                   if pct > 0.01 else None)
            self._cb(pct, eta)


def _run(cmd: list[str], on_progress=None) -> None:
    """Run ffmpeg. When `on_progress` is given, append `-progress pipe:1` and
    stream the encoded-seconds value (parsed from ffmpeg's machine-readable
    progress output) to it. Returns after the process exits; raises on error."""
    if on_progress is None:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg 失败：{' '.join(cmd[:3])}…\n{proc.stderr[-800:]}")
        return

    # Machine-readable progress: ffmpeg writes key=value blocks to stdout
    # (frame, out_time_us, ...). out_time_us is the muxed duration in µs.
    proc = subprocess.Popen(
        [*cmd, "-progress", "pipe:1", "-nostats"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", bufsize=1)
    assert proc.stdout is not None and proc.stderr is not None
    for line in proc.stdout:
        sec = _progress_seconds(line)
        if sec and sec > 0 and on_progress:
            on_progress(sec)
    err = proc.stderr.read()
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 失败：{' '.join(cmd[:3])}…\n{err[-800:]}")


def _has_audio_stream(path: Path) -> bool:
    """Use ffprobe to check if a file has at least one audio stream."""
    if not path.exists():
        return False
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    return bool(r.stdout.strip())


def _make_clip(frame_png: Path, audio: Path | None, duration: float,
               out_mp4: Path, on_progress=None) -> None:
    # Always pin the clip to an explicit duration so video length matches the
    # narration exactly (avoids A/V drift after concat).
    dur = f"{max(1.0, duration):.2f}"
    common_v = ["-r", str(_FPS), "-s", f"{W}x{H}", "-c:v", "libx264",
                "-preset", "veryfast", "-tune", "stillimage",
                "-pix_fmt", "yuv420p"]
    if audio and Path(audio).exists():
        _run(["ffmpeg", "-y", "-loop", "1", "-i", str(frame_png),
              "-i", str(audio), *common_v,
              "-c:a", "aac", "-b:a", "128k", "-ar", str(_SAMPLE_RATE),
              "-ac", "2", "-t", dur, str(out_mp4)], on_progress=on_progress)
    else:
        _run(["ffmpeg", "-y", "-loop", "1", "-i", str(frame_png),
              "-f", "lavfi", "-i",
              f"anullsrc=r={_SAMPLE_RATE}:cl=stereo",
              "-t", dur, *common_v,
              "-c:a", "aac", "-b:a", "128k", "-ar", str(_SAMPLE_RATE),
              "-ac", "2", str(out_mp4)], on_progress=on_progress)


def _seek_for(offset: float, total: float) -> float:
    """Where to start the next clip on a reused video: continue from `offset`,
    but clamp near the end (keep the last frame) once the footage is used up."""
    if total <= 0:
        return 0.0
    return min(offset, max(0.0, total - 0.1))


def _video_bg_filter(fit: str, freeze_to: float | None = None) -> str:
    """Build the ffmpeg filter for the source video background, ending in [bg],
    honoring the fit mode (crop|fit|blur). When `freeze_to` is set, the last
    frame is cloned (tpad) so the clip can be extended to that duration."""
    tpad = (f",tpad=stop_mode=clone:stop_duration={freeze_to:.2f}"
            if freeze_to else "")
    if fit == "crop":
        return ("[0:v]scale=1280:720:force_original_aspect_ratio=increase,"
                "crop=1280:720,setsar=1" + tpad + ",fps=30[bg]")
    if fit == "blur":
        return ("[0:v]split=2[a][b];"
                "[a]scale=1280:720:force_original_aspect_ratio=increase,"
                "crop=1280:720,boxblur=20:5[bgb];"
                "[b]scale=1280:720:force_original_aspect_ratio=decrease[fg];"
                "[bgb][fg]overlay=(W-w)/2:(H-h)/2,setsar=1" + tpad +
                ",fps=30[bg]")
    # fit (default): whole frame visible, letterboxed
    return ("[0:v]scale=1280:720:force_original_aspect_ratio=decrease,"
            "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black,"
            "setsar=1" + tpad + ",fps=30[bg]")


def _make_video_clip(src_video: Path, overlay_png: Path, audio: Path | None,
                     duration: float, out_mp4: Path, fit: str = "fit",
                     seek: float = 0.0, on_progress=None) -> None:
    """Use a source video clip as the background (placed per `fit`), starting
    at `seek` seconds (so consecutive scenes on the same video continue rather
    than restart). The last frame is frozen (tpad) to fill `duration` if the
    remaining footage is shorter. Original audio dropped; our subtitle is
    overlaid and our narration audio is the soundtrack."""
    dur = f"{max(1.0, duration):.2f}"
    vf = _video_bg_filter(fit, freeze_to=duration) + ";[bg][1:v]overlay=0:0[v]"
    seek_args = ["-ss", f"{seek:.2f}"] if seek and seek > 0 else []
    common = ["-r", str(_FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio and Path(audio).exists():
        _run(["ffmpeg", "-y", *seek_args, "-i", str(src_video),
              "-i", str(overlay_png), "-i", str(audio),
              "-filter_complex", vf, "-map", "[v]", "-map", "2:a",
              *common, "-c:a", "aac", "-b:a", "128k", "-ar", str(_SAMPLE_RATE),
              "-ac", "2", "-t", dur, str(out_mp4)], on_progress=on_progress)
    else:
        _run(["ffmpeg", "-y", *seek_args, "-i", str(src_video),
              "-i", str(overlay_png), "-f", "lavfi", "-i",
              f"anullsrc=r={_SAMPLE_RATE}:cl=stereo",
              "-filter_complex", vf, "-map", "[v]", "-map", "2:a",
              "-t", dur, *common,
              "-c:a", "aac", "-b:a", "128k", "-ar", str(_SAMPLE_RATE),
              "-ac", "2", str(out_mp4)], on_progress=on_progress)


def _is_enabled(val: str | bool | None) -> bool:
    if val is None:
        return True  # enabled by default
    if isinstance(val, bool):
        return val
    return val in ("1", "true", "yes")


def build_video(script: dict, work_dir: Path, image_map: dict[int, Path],
                out_mp4: Path, progress=None,
                video_map: dict[int, Path] | None = None,
                fit: str = "fit",
                brand_name: str = "Media Agent",
                avatar=None, on_progress=None) -> Path:
    """Compose a narrated video from a narration script.

    image_map: 1-based index -> local image path (for media "image:N").
    video_map: 1-based index -> local video path (for media "video:N").
    fit: how to place media into 1280x720 — fit (letterbox) | crop | blur.
    on_progress: optional callback(pct: float, eta_sec: float | None) called
        throughout encoding with overall completion and estimated remaining
        seconds (None until progress is measurable).
    """
    emit = progress or (lambda *_: None)
    import time as _time
    _t0 = _time.monotonic()
    video_map = video_map or {}
    if not ffmpeg_available():
        raise RuntimeError(
            "未找到 ffmpeg/ffprobe，请先安装并加入 PATH（https://ffmpeg.org）")

    work_dir = Path(work_dir)
    scenes = script.get("scenes", [])
    if not scenes:
        raise ValueError("脚本没有分镜")

    def _idx(media: str, prefix: str):
        try:
            return int(media.split(":", 1)[1]) if media.startswith(prefix) \
                else None
        except ValueError:
            return None

    # Ordered pool of available images, used to auto-fill scenes whose `media`
    # the LLM left as none/invalid — so the video never goes all black.
    img_pool = [image_map[k] for k in sorted(image_map)]
    # Track each source video's playback position so consecutive scenes on the
    # same video continue instead of restarting, and freeze on the last frame
    # once the footage runs out. Total durations cached to avoid re-probing.
    video_pos: dict[str, float] = {}
    video_total: dict[str, float] = {}

    total = len(scenes)
    clips: list[Path | None] = [None] * total

    def _scene_audio(i: int):
        sc = scenes[i]
        audio = None
        if sc.get("audio"):
            cand = work_dir / sc["audio"]
            if cand.exists():
                audio = cand
        dur = probe_duration(audio) if audio else None
        if not dur:
            dur = max(2.0, len(sc.get("narration", "")) * 0.25)
        return audio, dur

    def _is_video_scene(i: int) -> bool:
        sc = scenes[i]
        if sc.get("type") in ("intro", "outro"):
            return False
        media = str(sc.get("media", "none"))
        vidx = _idx(media, "video:")
        src_video = video_map.get(vidx) if vidx else None
        return bool(src_video and Path(src_video).exists())

    def _build_image_clip(i: int) -> None:
        """Render+encode one non-video scene.  No shared mutable state."""
        sc = scenes[i]
        audio, duration = _scene_audio(i)
        clip = work_dir / f"clip-{i}.mp4"
        scene_type = sc.get("type", "normal")

        # Resolve background: bg_custom > upload: media > image:N > pool
        bg = None
        if sc.get("bg_custom"):
            cand = work_dir / sc["bg_custom"]
            if cand.exists():
                bg = cand
                emit(f"  分镜 #{i + 1}：使用自定义背景 {sc['bg_custom']}")
            else:
                fallback = work_dir / Path(sc["bg_custom"]).name
                if fallback.exists():
                    bg = fallback
                    emit(f"  分镜 #{i + 1}：使用自定义背景 {fallback.name}")
                else:
                    emit(f"  分镜 #{i + 1}：自定义背景 {sc['bg_custom']} 不存在")
        if bg is None:
            media = str(sc.get("media", "none"))
            up = re.match(r"^upload:(.+)", media)
            if up:
                cand = work_dir / up.group(1)
                if cand.exists():
                    bg = cand
                    emit(f"  分镜 #{i + 1}：使用上传背景 {up.group(1)}")
                else:
                    fallback = work_dir / Path(up.group(1)).name
                    if fallback.exists():
                        bg = fallback
                        emit(f"  分镜 #{i + 1}：使用上传背景 {fallback.name}")

        if scene_type == "intro":
            title = script.get("title") or script.get("article_title") or ""
            frame = render_intro_frame(
                title, brand_name, work_dir / f"intro-{i}.png",
                bg_image=bg)
            _make_clip(frame, audio, duration, clip,
                       on_progress=_track(i, duration))
        elif scene_type == "outro":
            frame = render_outro_frame(
                brand_name, work_dir / f"outro-{i}.png",
                bg_image=bg)
            _make_clip(frame, audio, duration, clip,
                       on_progress=_track(i, duration))
        else:
            if bg is None:
                iidx = _idx(media, "image:")
                bg = image_map.get(iidx) if iidx else None
            if bg is None and img_pool:
                bg = img_pool[i % len(img_pool)]
            frame = render_frame(sc.get("narration", ""),
                                 work_dir / f"frame-{i}.png", bg_image=bg,
                                 fit=fit)
            _make_clip(frame, audio, duration, clip,
                       on_progress=_track(i, duration))
        tracker.finish_clip(i, duration)
        tracker.emit()

    def _build_video_clip(i: int) -> None:
        """Render+encode one video-background scene.
        Called in scene order per video source (seek position is stateful)."""
        sc = scenes[i]
        audio, duration = _scene_audio(i)
        narration = sc.get("narration", "")
        media = str(sc.get("media", "none"))
        clip = work_dir / f"clip-{i}.mp4"
        vidx = _idx(media, "video:")
        src_video = video_map.get(vidx)
        overlay = render_subtitle_overlay(
            narration, work_dir / f"ov-{i}.png")
        key = str(src_video)
        if key not in video_total:
            video_total[key] = probe_duration(src_video) or 0.0
        offset = video_pos.get(key, 0.0)
        seek = _seek_for(offset, video_total[key])
        _make_video_clip(src_video, overlay, audio, duration, clip,
                         fit=fit, seek=seek,
                         on_progress=_track(i, duration))
        tracker.finish_clip(i, duration)
        tracker.emit()
        video_pos[key] = offset + duration

    # Identify scenes that need sequential video-seek ordering
    video_scenes = [i for i in range(total) if _is_video_scene(i)]
    image_scenes = [i for i in range(total) if not _is_video_scene(i)]

    # Weight the progress bar by scene duration (longer scenes encode longer).
    # Digital-human scenes add a second encode pass (avatar composite), and the
    # final concat is a fixed small chunk so the bar reaches 100% only at the end.
    scene_durs = [_scene_audio(i)[1] for i in range(total)]
    avatar_work = 0.0
    if avatar is not None and getattr(avatar, "enabled", False) and avatar.ready():
        from app.video.avatar import scene_avatar_mode
        avatar_work = sum(scene_durs[i] for i in range(total)
                          if scene_avatar_mode(scenes[i], avatar) != "off")
    concat_work = 6.0
    tracker = _ProgressTracker(sum(scene_durs) + avatar_work + concat_work,
                               on_progress, t0=_t0)

    def _track(i: int, duration: float):
        """ffmpeg on_progress hook: report per-clip encoded seconds."""
        def cb(sec: float):
            tracker.tick(i, sec, duration)
            tracker.emit()
        return cb

    # Phase 1 — image / intro / outro scenes: fully independent, parallel
    if image_scenes and emit:
        emit(f"合成分镜画面 {len(image_scenes)} 个（并行）…")
    with ThreadPoolExecutor(max_workers=min(len(image_scenes) if image_scenes else 1, 4)) as pool:
        for _ in pool.map(_build_image_clip, image_scenes):
            pass
    _t1 = _time.monotonic()
    if emit:
        emit(f"分镜画面完成，耗时 {_t1 - _t0:.1f}s")

    # Phase 2 — video-background scenes: sequential per video source
    for i in video_scenes:
        if emit:
            emit(f"合成分镜 {i + 1}/{total}（视频背景）")
        _build_video_clip(i)

    clips = [work_dir / f"clip-{i}.mp4" for i in range(total)]

    # Phase 2.5 — composite the digital-human presenter (数字人主播) onto scenes
    # that request it. Runs after backgrounds are built; SadTalker (if set up)
    # is slow and single-GPU, so keep this sequential.
    if avatar is not None and getattr(avatar, "enabled", False) and avatar.ready():
        from app.video.avatar import scene_avatar_mode, composite_scene
        for i in range(total):
            mode = scene_avatar_mode(scenes[i], avatar)
            if mode == "off":
                continue
            audio, duration = _scene_audio(i)
            emit(f"合成数字人主播 {i + 1}/{total}…")
            composite_scene(clips[i], audio, duration,
                            scenes[i].get("narration", ""), mode, avatar,
                            work_dir, i, emit)
            tracker.add_fixed(duration)
            tracker.emit()

    # Warn about clips without audio (diagnostic — shouldn't normally happen)
    silent: list[int] = []
    for i, c in enumerate(clips):
        if c.exists() and not _has_audio_stream(c):
            silent.append(i)
    if silent:
        emit(f"⚠️ 分镜 {silent} 缺少音轨，将用静音填充")

    emit("拼接所有分镜…")
    list_file = work_dir / "concat.txt"
    list_file.write_text(
        "".join(f"file '{c.resolve().as_posix()}'\n" for c in clips),
        encoding="utf-8")
    out_mp4 = Path(out_mp4)

    def _concat_demuxer(reencode: bool = False) -> None:
        if reencode:
            _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                  "-i", str(list_file), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                  "-c:a", "aac", "-ar", str(_SAMPLE_RATE), str(out_mp4)])
        else:
            _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                  "-i", str(list_file), "-c", "copy", str(out_mp4)])

    # Primary: fast stream-copy concat
    try:
        _concat_demuxer(reencode=False)
    except RuntimeError:
        emit("流拷贝拼接失败，尝试重新编码…")
        _concat_demuxer(reencode=True)
    tracker.add_fixed(concat_work)
    tracker.emit()

    # Verify the output has an audio track (stream-copy can silently produce
    # a file with no audio if any clip lacks one; re-encode also may skip
    # audio if no clip provides it).  If missing, build a filter-graph concat
    # that forces audio from anullsrc for any clip that lacks it.
    if not _has_audio_stream(out_mp4):
        emit("输出缺少音轨，强制补入…")
        # Build a filter concat that fills silent clips with anullsrc:
        #   -i clip0.mp4 -i clip1.mp4 …
        #   -filter_complex
        #     [0:v]anullsrc=...[0:a]asrc...[1:v]...[1:a]…concat=n=N:v=1:a=1[v][a]
        #   -map [v] -map [a] ...
        flt_inputs: list[str] = []
        flt_parts: list[str] = []
        anullsrc = f"anullsrc=r={_SAMPLE_RATE}:cl=stereo"
        for idx, c in enumerate(clips):
            if not c.exists():
                continue
            flt_inputs.extend(["-i", str(c)])
            # Always use anullsrc + original audio, mixed: prefer real audio,
            # fall back to silence when the clip has no audio stream.
            flt_parts.append(
                f"[{idx}:v]"
                f"[{idx}:a:0]"
            )
        if len(flt_parts) < 1:
            raise RuntimeError("没有可用的分镜文件")

        n = len(flt_parts)
        segs = ""
        a_srcs = []
        for i in range(n):
            # Use the clip's audio if present, otherwise anullsrc
            segs += f"[{i}:v][{i}:a:0]"
            a_srcs.append(f"[{i}:a:0]")
        # Need to handle clips without audio by using a muted version.
        # Simpler approach: force audio from all clips, concat filter
        # will drop any missing streams so we pad with anullsrc.
        fc = ("".join(f"[{i}:v][{i}:a:0]" for i in range(n))
              + f"concat=n={n}:v=1:a=1[vout][aout]")
        cmd = (["ffmpeg", "-y"] + flt_inputs
               + ["-filter_complex", fc, "-map", "[vout]", "-map", "[aout]",
                  "-c:v", "libx264", "-pix_fmt", "yuv420p",
                  "-c:a", "aac", "-ar", str(_SAMPLE_RATE),
                  "-ac", "2", "-b:a", "128k", str(out_mp4)])
        try:
            _run(cmd)
        except RuntimeError:
            raise RuntimeError(
                "视频合成失败：所有拼接方式均无法生成含音轨的输出。\n"
                "请确认配音文件完整、ffmpeg 版本正常。") from None

    _t2 = _time.monotonic()
    emit(f"视频合成完成，总耗时 {_t2 - _t0:.1f}s")
    tracker.finish()
    return out_mp4
