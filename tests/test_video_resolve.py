"""Regression: the video stage must resolve article images whether the draft
stored them as absolute /media/ paths OR file-relative ../../media/ paths
(as the rewriter/localizer writes them). Previously only /media/ resolved, so
the composed video ignored per-scene backgrounds."""
from pathlib import Path

from app.config import Config
from app.pipeline.video import _resolve_media


def _setup(tmp_path: Path):
    cfg = Config(data_dir=tmp_path, llm_provider="mock", image_provider="mock")
    cfg.ensure_dirs()
    folder = "abc123"
    d = cfg.media_dir / folder
    d.mkdir(parents=True, exist_ok=True)
    pic = d / "pic.png"
    pic.write_bytes(b"\x89PNG\r\n\x1a\n")  # non-empty placeholder
    return cfg, folder, pic


def test_resolve_absolute_media_path(tmp_path):
    cfg, folder, pic = _setup(tmp_path)
    got = _resolve_media([f"/media/{folder}/pic.png"], cfg)
    assert got.get(1) == pic


def test_resolve_relative_media_path(tmp_path):
    cfg, folder, pic = _setup(tmp_path)
    # This is what the localizer writes into draft bodies.
    got = _resolve_media([f"../../media/{folder}/pic.png"], cfg)
    assert got.get(1) == pic


def test_resolve_mixed_and_unresolvable(tmp_path):
    cfg, folder, pic = _setup(tmp_path)
    got = _resolve_media([
        f"../../media/{folder}/pic.png",   # 1 → resolves
        f"/media/{folder}/missing.png",    # 2 → not on disk
        "relative/nope.png",               # 3 → unsupported
    ], cfg)
    assert got.get(1) == pic
    assert 2 not in got and 3 not in got
