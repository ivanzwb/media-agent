from PIL import Image

from app.video.builder import (
    render_frame, render_subtitle_overlay, _wrap, _font)


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


def test_wrap_breaks_long_text():
    from PIL import ImageDraw
    canvas = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(canvas)
    font = _font(40)
    lines = _wrap(draw, "字" * 80, font, 600)
    assert len(lines) > 1
    assert "".join(lines) == "字" * 80
