from PIL import Image

from app.video.builder import (
    render_frame, render_subtitle_overlay, _wrap, _font,
    _contain, _cover, _fit_image, _video_bg_filter, _seek_for)


def test_render_frame_produces_720p_png(tmp_path):
    out = render_frame("这是一段中文字幕，用于测试换行与渲染。" * 3,
                       tmp_path / "frame.png")
    assert out.exists()
    with Image.open(out) as im:
        assert im.size == (1280, 720)


def test_render_frame_with_background(tmp_path):
    bg = tmp_path / "bg.png"
    Image.new("RGB", (400, 300), (200, 50, 50)).save(bg)
    out = render_frame("标题", tmp_path / "frame.png", bg_image=bg)
    with Image.open(out) as im:
        assert im.size == (1280, 720)


def test_render_frame_missing_background_is_ok(tmp_path):
    out = render_frame("无背景", tmp_path / "f.png",
                       bg_image=tmp_path / "nope.png")
    assert out.exists()


def test_subtitle_overlay_is_transparent_on_top(tmp_path):
    out = render_subtitle_overlay("视频片段字幕", tmp_path / "ov.png")
    with Image.open(out) as im:
        assert im.mode == "RGBA"
        assert im.size == (1280, 720)
        # top is fully transparent; bottom band has visible (opaque) pixels
        assert im.getpixel((640, 20))[3] == 0
        assert im.getpixel((640, 700))[3] > 0


def test_fit_modes_produce_720p_canvas():
    # a tall (portrait) image: crop would lose top/bottom, fit/blur keep it all
    tall = Image.new("RGB", (300, 900), (10, 120, 200))
    for mode in ("fit", "crop", "blur"):
        out = _fit_image(tall, mode)
        assert out.size == (1280, 720)


def test_contain_keeps_whole_image_within_bounds():
    tall = Image.new("RGB", (300, 900))
    c = _contain(tall)
    assert c.width <= 1280 and c.height <= 720
    # portrait aspect preserved (height is the limiting side -> 720)
    assert c.height == 720
    # cover instead fills the frame (one dim hits the target, other overflows)
    cov = _cover(tall)
    assert cov.size == (1280, 720)


def test_render_frame_fit_mode(tmp_path):
    bg = tmp_path / "bg.png"
    Image.new("RGB", (300, 900), (200, 50, 50)).save(bg)
    out = render_frame("竖屏视频", tmp_path / "f.png", bg_image=bg, fit="fit")
    with Image.open(out) as im:
        assert im.size == (1280, 720)


def test_video_bg_filter_modes():
    assert "pad=1280:720" in _video_bg_filter("fit")          # letterbox
    assert "crop=1280:720" in _video_bg_filter("crop")        # fill+crop
    assert "boxblur" in _video_bg_filter("blur")              # blurred bg
    # all end in the [bg] label for chaining with the subtitle overlay
    for m in ("fit", "crop", "blur"):
        assert _video_bg_filter(m).rstrip().endswith("[bg]")


def test_video_bg_filter_freeze_adds_tpad():
    # freeze clones the last frame so a clip can be extended to a duration
    for m in ("fit", "crop", "blur"):
        f = _video_bg_filter(m, freeze_to=5.0)
        assert "tpad=stop_mode=clone:stop_duration=5.00" in f
        assert f.rstrip().endswith("[bg]")
    # no freeze -> no tpad
    assert "tpad" not in _video_bg_filter("fit")


def test_seek_for_continues_then_freezes():
    # 15s video, 5s scenes
    assert _seek_for(0.0, 15.0) == 0.0      # first scene from start
    assert _seek_for(5.0, 15.0) == 5.0      # continue
    assert _seek_for(10.0, 15.0) == 10.0    # continue
    # exhausted -> clamp near the end (freeze last frame)
    assert _seek_for(15.0, 15.0) == 14.9
    assert _seek_for(20.0, 15.0) == 14.9
    # unknown total -> start from 0
    assert _seek_for(8.0, 0.0) == 0.0


def test_wrap_breaks_long_text():
    from PIL import ImageDraw
    canvas = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(canvas)
    font = _font(40)
    lines = _wrap(draw, "字" * 80, font, 600)
    assert len(lines) > 1
    assert "".join(lines) == "字" * 80
