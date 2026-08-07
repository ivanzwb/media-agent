"""Video synthesis must localize the script's remote media into
data/media/<hash>/ (exactly like article content localization) and rewrite
script["images"] / script["videos"] to local /media/... paths, so re-running
the synthesis never re-downloads remote images/videos.

Covered here:
  - remote images AND videos referenced by a narration script get downloaded
    into a stable data/media/<hash>/ folder and script refs rewritten;
  - local refs (/media/, ../../media/, /images/, /videos/) pass through
    untouched;
  - failed downloads keep the original remote URL (build degrades gracefully
    via the work-dir fallback in _resolve_media);
  - a script that is already fully local is a no-op (idempotent re-runs);
  - after localization, _resolve_media resolves the rewritten refs to real
    local files without any download;
  - build_explainer_video persists the rewritten script.json."""
import hashlib
import json as _json
from pathlib import Path

from app.config import Config
from app.pipeline.video import _resolve_media, localize_script_media


def _cfg(tmp_path: Path) -> Config:
    cfg = Config(data_dir=tmp_path, llm_provider="mock", image_provider="mock")
    cfg.ensure_dirs()
    return cfg


def _script(images=None, videos=None, scenes=None) -> dict:
    return {"scenes": scenes or [], "images": images or [], "videos": videos or []}


def _folder(*remote_urls: str) -> str:
    return hashlib.sha1("|".join(sorted(remote_urls)).encode()).hexdigest()[:16]


class _FakeDownload:
    """Mimics a strategy download fn used by _download_set: only fetches
    http(s) URLs (relative/local/absolute paths come back None — the real
    downloader also refuses them), writing dest/<name>-<idx>.ext."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []

    def __call__(self, url, dest, idx, emit, source_url=None):
        if not url.startswith(("http://", "https://")):
            return None
        self.calls.append(url)
        p = dest / f"{self.name}-{idx}.ext"
        p.write_bytes(b"FAKE")
        return p


def _patch(monkeypatch, **kwargs):
    """Bind fake downloaders into app.pipeline.video's namespace so the
    localize_script_media function (which reads module globals) uses them."""
    import app.pipeline.video as video
    for name, fake in kwargs.items():
        if fake is None:
            monkeypatch.setattr(video, name, lambda *a, **k: None)
        else:
            monkeypatch.setattr(video, name, fake)
    return video


def test_localize_remote_images_rewritten(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    fake = _FakeDownload("img")
    _patch(monkeypatch, _download_image=fake)

    script = _script(images=["https://example.com/a.png"])
    assert localize_script_media(script, cfg) is True

    folder = _folder("https://example.com/a.png")
    assert script["images"] == [f"/media/{folder}/img-1.ext"]
    media_file = cfg.media_dir / folder / "img-1.ext"
    assert media_file.exists() and media_file.read_bytes() == b"FAKE"
    assert fake.calls == ["https://example.com/a.png"]


def test_localize_mixed_local_and_remote(tmp_path, monkeypatch):
    """Local refs in any supported form pass through untouched."""
    cfg = _cfg(tmp_path)
    fake = _FakeDownload("img")
    _patch(monkeypatch, _download_image=fake)

    script = _script(images=[
        "https://cdn.example.com/hero.png",   # remote → localized
        "/media/abc123/pic.png",                 # local absolute
        "../../media/abc123/rel.png",             # file-relative
        "/images/cover.jpg",                      # images dir
        "/videos/clip.mp4",                       # videos dir
    ])
    assert localize_script_media(script, cfg) is True

    assert script["images"][0].startswith("/media/")
    assert script["images"][1:] == [
        "/media/abc123/pic.png",
        "../../media/abc123/rel.png",
        "/images/cover.jpg",
        "/videos/clip.mp4",
    ]
    # only the remote URL was actually downloaded
    assert fake.calls == ["https://cdn.example.com/hero.png"]


def test_localize_remote_videos_rewritten(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    fake = _FakeDownload("vid")
    _patch(monkeypatch, _download_video=fake)

    script = _script(videos=["https://example.com/b.mp4"])
    assert localize_script_media(script, cfg) is True

    folder = _folder("https://example.com/b.mp4")
    assert script["videos"] == [f"/media/{folder}/vid-1.ext"]
    assert (cfg.media_dir / folder / "vid-1.ext").exists()


def test_localize_no_remote_urls_is_noop(tmp_path, monkeypatch):
    """Script containing only local media: nothing is downloaded or changed."""
    cfg = _cfg(tmp_path)
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError("must not download")

    _patch(monkeypatch, _download_image=boom, _download_video=boom)
    script = _script(images=["/media/abc/pic.png", "/images/cov.jpg"])
    assert localize_script_media(script, cfg) is False
    assert calls == []


def test_localize_failure_keeps_remote_url(tmp_path, monkeypatch):
    """Failed download → URL stays remote (build falls back to work-dir)."""
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, _download_image=None)

    script = _script(images=["https://example.com/a.png"])
    assert localize_script_media(script, cfg) is False
    assert script["images"] == ["https://example.com/a.png"]


def test_localize_idempotent(tmp_path, monkeypatch):
    """A script already pointing at /media/... is left untouched."""
    cfg = _cfg(tmp_path)
    fake = _FakeDownload("img")
    _patch(monkeypatch, _download_image=fake)

    local = f"/media/{'a' * 16}/img-1.png"
    script = _script(images=[local])
    assert localize_script_media(script, cfg) is False
    assert script["images"] == [local]
    assert fake.calls == []


def test_after_localize_resolve_without_network(tmp_path, monkeypatch):
    """Rewritten refs resolve to real files via _resolve_media (no download)."""
    cfg = _cfg(tmp_path)
    fake = _FakeDownload("img")
    _patch(monkeypatch, _download_image=fake)

    script = _script(images=["https://example.com/a.png",
                             "../../media/abc123/pic.png"])
    assert localize_script_media(script, cfg) is True

    # the file-relative local ref must exist for resolution below
    local_dir = cfg.media_dir / "abc123"
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / "pic.png").write_bytes(b"LOCAL")

    got = _resolve_media(script["images"], cfg)  # download=False
    assert len(got) == 2
    assert got[1].read_bytes() == b"FAKE"
    assert got[2] == local_dir / "pic.png"


def test_build_explainer_video_persists_localized_script(tmp_path, monkeypatch):
    """build_explainer_video persists the rewritten script.json so re-runs
    load local /media/ refs instead of the original remote URLs."""
    from app.pipeline.video import build_explainer_video

    cfg = _cfg(tmp_path)
    out_dir = cfg.videos_dir / "draft-7"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "script.json").write_text(
        _json.dumps(_script(
            scenes=[{"type": "normal", "narration": "hi", "media": "image:1"}],
            images=["https://cdn.example.com/hero.png"]),
            ensure_ascii=False, indent=2), encoding="utf-8")

    import app.pipeline.video as video
    fake = _FakeDownload("img")
    _patch(monkeypatch, _download_image=fake)
    monkeypatch.setattr(video, "build_video",
                        lambda *a, **k: out_dir / "video.mp4")

    build_explainer_video(7, cfg)

    written = _json.loads((out_dir / "script.json").read_text(encoding="utf-8"))
    ref = written["images"][0]
    assert ref.startswith("/media/")
    assert "cdn.example.com" not in ref
    # the localized file is on disk under data/media/<folder>/
    folder, name = ref[len("/media/"):].split("/", 1)
    assert (cfg.media_dir / folder / name).exists()