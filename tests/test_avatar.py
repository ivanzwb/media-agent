"""Tests for the digital-human presenter (数字人主播) compositing logic.

ffmpeg is mocked so these run without ffmpeg or any ML model — they validate
mode resolution, the SadTalker→static fallback, and that compositing replaces
the clip with the right ffmpeg command shape.
"""
from pathlib import Path

import app.video.avatar as av


def _spec(tmp_path, **kw):
    img = tmp_path / "presenter.png"
    img.write_bytes(b"\x89PNG\r\n")
    base = dict(enabled=True, image=img, provider="still", position="pip")
    base.update(kw)
    return av.AvatarSpec(**base)


def test_scene_mode_default_and_overrides(tmp_path):
    spec = _spec(tmp_path, position="pip")
    assert av.scene_avatar_mode({}, spec) == "pip"                 # global default
    assert av.scene_avatar_mode({"avatar": "full"}, spec) == "full"  # per-scene
    assert av.scene_avatar_mode({"avatar": "off"}, spec) == "off"
    assert av.scene_avatar_mode({"avatar": ""}, spec) == "pip"


def test_scene_mode_off_when_not_ready(tmp_path):
    assert av.scene_avatar_mode({}, av.AvatarSpec(enabled=False)) == "off"
    # enabled but no image file → not ready
    missing = av.AvatarSpec(enabled=True, image=tmp_path / "nope.png")
    assert av.scene_avatar_mode({}, missing) == "off"


def test_sadtalker_available_false(tmp_path):
    assert av.sadtalker_available(av.AvatarSpec()) is False
    # dir without inference.py → unavailable
    assert av.sadtalker_available(av.AvatarSpec(sadtalker_dir=str(tmp_path))) is False


def test_talking_head_none_without_sadtalker(tmp_path):
    spec = _spec(tmp_path, provider="sadtalker", sadtalker_dir=None)
    assert av.generate_talking_head(tmp_path / "a.wav", spec, tmp_path, 0) is None


def test_talking_head_uses_managed_python_and_checkpoint_dir(tmp_path,
                                                             monkeypatch):
    source = tmp_path / "runtime" / "sadtalker-src"
    source.mkdir(parents=True)
    (source / "inference.py").write_text("", encoding="utf-8")
    python = tmp_path / "runtime" / "python.exe"
    python.write_bytes(b"python")
    checkpoints = tmp_path / "models" / "checkpoints"
    checkpoints.mkdir(parents=True)
    for name in (
        "SadTalker_V0.0.2_256.safetensors",
        "mapping_00109-model.pth.tar",
        "mapping_00229-model.pth.tar",
    ):
        (checkpoints / name).write_bytes(b"model")
    spec = _spec(
        tmp_path, provider="sadtalker", sadtalker_dir=str(source),
        sadtalker_python=str(python), checkpoint_dir=str(checkpoints))
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"wav")
    monkeypatch.setattr(av, "_to_wav", lambda _audio, out: out)
    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(([str(v) for v in cmd], kwargs))
        result_dir = Path(cmd[cmd.index("--result_dir") + 1])
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "talking.mp4").write_bytes(b"video")
        return Result()

    monkeypatch.setattr(av.subprocess, "run", fake_run)
    monkeypatch.setattr(
        "app.video.sadtalker_setup.runtime_target",
        lambda: type("Target", (), {"device": "cpu", "slow": False})())
    result = av.generate_talking_head(audio, spec, tmp_path, 0)

    assert result and result.name == "talking.mp4"
    cmd, kwargs = calls[0]
    assert cmd[0] == str(python)
    assert cmd[cmd.index("--checkpoint_dir") + 1] == str(checkpoints)
    assert cmd[cmd.index("--device") + 1] == "cpu"
    assert kwargs["cwd"] == str(source)


def test_composite_pip_static_replaces_clip(tmp_path, monkeypatch):
    import app.video.builder as builder

    def fake_sub(text, out):
        assert text == "hi"
        Path(out).write_bytes(b"sub")
        return Path(out)

    monkeypatch.setattr(builder, "render_subtitle_overlay", fake_sub)
    spec = _spec(tmp_path, position="pip")
    clip = tmp_path / "clip-0.mp4"
    clip.write_bytes(b"orig")
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=None):
        calls.append([str(c) for c in cmd])
        Path(cmd[-1]).write_bytes(b"composited")

    monkeypatch.setattr(av, "_run", fake_run)
    av.composite_scene(clip, None, 3.0, "hi", "pip", spec, tmp_path, 0)

    assert clip.read_bytes() == b"composited"           # replaced in place
    joined = " ".join(calls[-1])
    assert joined.count("overlay=") == 2                 # PiP, then subtitle
    assert "[av][2:v]overlay=0:0[v]" in joined           # subtitle is topmost
    assert str(spec.image) in calls[-1]                  # static image input
    assert str(tmp_path / "av-sub-0.png") in calls[-1]   # subtitle input


def test_composite_full_static_uses_subtitle(tmp_path, monkeypatch):
    import app.video.builder as builder

    def fake_sub(text, out):
        Path(out).write_bytes(b"sub")
        return Path(out)

    monkeypatch.setattr(builder, "render_subtitle_overlay", fake_sub)
    calls: list[list[str]] = []
    monkeypatch.setattr(av, "_run",
                        lambda cmd, timeout=None: (calls.append([str(c) for c in cmd]),
                                                   Path(cmd[-1]).write_bytes(b"composited")))
    spec = _spec(tmp_path, position="full")
    clip = tmp_path / "clip-0.mp4"
    clip.write_bytes(b"orig")
    av.composite_scene(clip, None, 3.0, "旁白", "full", spec, tmp_path, 0)

    assert clip.read_bytes() == b"composited"
    assert any("overlay=0:0" in " ".join(c) for c in calls)


def test_avatar_spec_from_config(tmp_path):
    from app.config import Config
    from app.pipeline.video import _avatar_spec
    noop = lambda *_a, **_k: None  # noqa: E731

    # disabled → None
    assert _avatar_spec(Config(data_dir=tmp_path, avatar_enabled=False), noop) is None
    # enabled but no image set → None
    assert _avatar_spec(Config(data_dir=tmp_path, avatar_enabled=True,
                               avatar_image=""), noop) is None
    # enabled but image file missing → None
    cfg = Config(data_dir=tmp_path, avatar_enabled=True, avatar_image="nope.png")
    cfg.ensure_dirs()
    assert _avatar_spec(cfg, noop) is None
    # valid → spec with resolved absolute image + position
    cfg2 = Config(data_dir=tmp_path, avatar_enabled=True, avatar_image="p.png",
                  avatar_position="full", avatar_provider="still")
    cfg2.ensure_dirs()
    (cfg2.avatar_dir / "p.png").write_bytes(b"\x89PNG")
    spec = _avatar_spec(cfg2, noop)
    assert spec is not None and spec.enabled and spec.ready()
    assert spec.position == "full"
    assert spec.image == cfg2.avatar_dir / "p.png"


def test_composite_noop_when_off_or_not_ready(tmp_path, monkeypatch):
    called: list[int] = []
    monkeypatch.setattr(av, "_run", lambda *a, **k: called.append(1))
    clip = tmp_path / "clip-0.mp4"
    clip.write_bytes(b"orig")
    # mode off
    av.composite_scene(clip, None, 3.0, "x", "off", _spec(tmp_path), tmp_path, 0)
    # not ready (missing image)
    av.composite_scene(clip, None, 3.0, "x", "pip",
                       av.AvatarSpec(enabled=True, image=tmp_path / "no.png"),
                       tmp_path, 0)
    assert clip.read_bytes() == b"orig"
    assert not called
