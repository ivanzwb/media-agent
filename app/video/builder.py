from __future__ import annotations

import os
import shutil
import subprocess
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
                       duration_sec: int = 4) -> Path:
    """Render an intro title frame: brand name + article title on a
    dark gradient background. The frame stays on screen for `duration_sec`
    (used only for clip timing)."""
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGB", (W, H), _BG)

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
                       duration_sec: int = 4) -> Path:
    """Render an outro frame: thank-you message + brand name on a dark
    background. The frame stays on screen for `duration_sec`."""
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGB", (W, H), _BG)
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


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 失败：{' '.join(cmd[:3])}…\n{proc.stderr[-800:]}")


def _make_clip(frame_png: Path, audio: Path | None, duration: float,
               out_mp4: Path) -> None:
    # Always pin the clip to an explicit duration so video length matches the
    # narration exactly (avoids A/V drift after concat).
    dur = f"{max(1.0, duration):.2f}"
    common_v = ["-r", str(_FPS), "-s", f"{W}x{H}", "-c:v", "libx264",
                "-tune", "stillimage", "-pix_fmt", "yuv420p"]
    if audio and Path(audio).exists():
        _run(["ffmpeg", "-y", "-loop", "1", "-i", str(frame_png),
              "-i", str(audio), *common_v,
              "-c:a", "aac", "-b:a", "128k", "-ar", str(_SAMPLE_RATE),
              "-ac", "2", "-t", dur, str(out_mp4)])
    else:
        _run(["ffmpeg", "-y", "-loop", "1", "-i", str(frame_png),
              "-f", "lavfi", "-i",
              f"anullsrc=r={_SAMPLE_RATE}:cl=stereo",
              "-t", dur, *common_v,
              "-c:a", "aac", "-b:a", "128k", "-ar", str(_SAMPLE_RATE),
              "-ac", "2", str(out_mp4)])


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
                     seek: float = 0.0) -> None:
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
              "-ac", "2", "-t", dur, str(out_mp4)])
    else:
        _run(["ffmpeg", "-y", *seek_args, "-i", str(src_video),
              "-i", str(overlay_png), "-f", "lavfi", "-i",
              f"anullsrc=r={_SAMPLE_RATE}:cl=stereo",
              "-filter_complex", vf, "-map", "[v]", "-map", "2:a",
              "-t", dur, *common,
              "-c:a", "aac", "-b:a", "128k", "-ar", str(_SAMPLE_RATE),
              "-ac", "2", str(out_mp4)])


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
                brand_name: str = "Media Agent") -> Path:
    """Compose a narrated video from a narration script.

    image_map: 1-based index -> local image path (for media "image:N").
    video_map: 1-based index -> local video path (for media "video:N").
    fit: how to place media into 1280x720 — fit (letterbox) | crop | blur.
    """
    emit = progress or (lambda *_: None)
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

        if scene_type == "intro":
            title = script.get("title") or script.get("article_title") or ""
            frame = render_intro_frame(
                title, brand_name, work_dir / f"intro-{i}.png")
            _make_clip(frame, audio, duration, clip)
        elif scene_type == "outro":
            frame = render_outro_frame(
                brand_name, work_dir / f"outro-{i}.png")
            _make_clip(frame, audio, duration, clip)
        else:
            iidx = _idx(str(sc.get("media", "none")), "image:")
            bg = image_map.get(iidx) if iidx else None
            if bg is None and img_pool:
                bg = img_pool[i % len(img_pool)]
            frame = render_frame(sc.get("narration", ""),
                                 work_dir / f"frame-{i}.png", bg_image=bg,
                                 fit=fit)
            _make_clip(frame, audio, duration, clip)

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
                         fit=fit, seek=seek)
        video_pos[key] = offset + duration

    # Identify scenes that need sequential video-seek ordering
    video_scenes = [i for i in range(total) if _is_video_scene(i)]
    image_scenes = [i for i in range(total) if not _is_video_scene(i)]

    # Phase 1 — image / intro / outro scenes: fully independent, parallel
    if image_scenes and emit:
        emit(f"合成分镜画面 {len(image_scenes)} 个（并行）…")
    with ThreadPoolExecutor(max_workers=min(len(image_scenes) if image_scenes else 1, 4)) as pool:
        for _ in pool.map(_build_image_clip, image_scenes):
            pass

    # Phase 2 — video-background scenes: sequential per video source
    for i in video_scenes:
        if emit:
            emit(f"合成分镜 {i + 1}/{total}（视频背景）")
        _build_video_clip(i)

    clips = [work_dir / f"clip-{i}.mp4" for i in range(total)]

    emit("拼接所有分镜…")
    list_file = work_dir / "concat.txt"
    list_file.write_text(
        "".join(f"file '{c.resolve().as_posix()}'\n" for c in clips),
        encoding="utf-8")
    out_mp4 = Path(out_mp4)
    try:
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
              "-i", str(list_file), "-c", "copy", str(out_mp4)])
    except RuntimeError:
        # Fallback: re-encode during concat for stream-compat issues.
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
              "-i", str(list_file), "-c:v", "libx264", "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-ar", str(_SAMPLE_RATE), str(out_mp4)])
    emit("视频合成完成")
    return out_mp4
