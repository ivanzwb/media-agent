"""Regression: the video stage must resolve article images whether the draft
stored them as absolute /media/ paths OR file-relative ../../media/ paths
(as the rewriter/localizer writes them). Previously only /media/ resolved, so
the composed video ignored per-scene backgrounds.

Also covers remote http(s) media: with download=True, remote images AND videos
(draft #507 regression: video backgrounds referenced as remote blog .mp4 URLs
were silently dropped, so those scenes fell back to the image branch)."""
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


# ---------------------------------------------------------------------------
# Remote http(s) media: download=True must fetch into the work dir.
# ---------------------------------------------------------------------------

def _work(tmp_path: Path) -> Path:
    w = tmp_path / "work"
    w.mkdir(parents=True, exist_ok=True)
    return w


def test_resolve_remote_video_downloads(tmp_path, monkeypatch):
    """kind="video" + download=True fetches a remote .mp4 into the work dir
    (draft #507: remote blog video URLs must become scene backgrounds)."""
    cfg, folder, pic = _setup(tmp_path)
    work = _work(tmp_path)
    fake = work / "dl-abc123" / "vid-1.mp4"

    def fake_download(url, w, emit):
        assert url.startswith("https://blogs.nvidia.com/")
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_bytes(b"FAKEVIDEO")
        return fake

    monkeypatch.setattr("app.pipeline.video._download_remote_video",
                        fake_download)
    got = _resolve_media(
        ["https://blogs.nvidia.com/wp/x.mp4?_=1"], cfg,
        work=work, download=True, kind="video")
    assert got.get(1) == fake


def test_resolve_remote_video_no_download_stays_empty(tmp_path, monkeypatch):
    """Without download=True, remote video URLs must not be touched (keeps
    local-only builds free of network fetches)."""
    cfg, folder, pic = _setup(tmp_path)
    work = _work(tmp_path)
    monkeypatch.setattr("app.pipeline.video._download_remote_video",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not be called")))
    got = _resolve_media(["https://blogs.nvidia.com/wp/x.mp4"], cfg,
                         work=work, download=False, kind="video")
    assert got == {}


def test_resolve_remote_image_downloads(tmp_path, monkeypatch):
    """kind="image" (default) + download=True still fetches remote images."""
    cfg, folder, pic = _setup(tmp_path)
    work = _work(tmp_path)
    fake = work / "dl-abc123.jpg"

    def fake_download(url, w, emit):
        assert url == "https://example.com/a.png"
        fake.write_bytes(b"FAKEIMG")
        return fake

    monkeypatch.setattr("app.pipeline.video._download_remote_image",
                        fake_download)
    got = _resolve_media(["https://example.com/a.png"], cfg,
                         work=work, download=True)
    assert got.get(1) == fake
