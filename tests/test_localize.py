from datetime import datetime, timezone

from app.config import Config
from app.models import Article
from app.pipeline import localize as loc
from app.pipeline.localize import (
    localize_article, local_media_file, normalize_url)


def test_normalize_url_handles_unicode_and_scheme():
    # non-ASCII path gets percent-encoded
    out = normalize_url("https://x.com/图片/封面 图.png")
    assert out is not None and out.startswith("https://x.com/")
    assert "%" in out and " " not in out
    out.encode("ascii")  # must be ascii-safe now
    # protocol-relative gets https
    assert normalize_url("//cdn.com/a.png") == "https://cdn.com/a.png"
    # relative / unsupported -> None
    assert normalize_url("/imgs/a.png") is None
    assert normalize_url("data:image/png;base64,xxxx") is None
    assert normalize_url("") is None
    # already-encoded stays intact (no double-encoding)
    assert normalize_url("https://x.com/a%20b.png") == "https://x.com/a%20b.png"
    # HTML entities are decoded (issue #1: &amp; -> & to avoid 400)
    assert normalize_url(
        "https://images.ctfassets.net/x/image.png?fm=webp&amp;w=3840&amp;q=70"
    ) == "https://images.ctfassets.net/x/image.png?fm=webp&w=3840&q=70"


def _article():
    return Article(
        title="t", content_md="c", url="https://x.com/a", source_name="X",
        source_type="scrape", published_at=None,
        images=["https://img.cdn/a.png", "https://img.cdn/b.jpg"],
        raw_summary=None, fetched_at=datetime.now(timezone.utc), topic="AI",
        videos=[])


class _Resp:
    content = b"\x89PNG\r\n\x1a\nfake"
    headers = {"content-type": "image/png"}

    def raise_for_status(self):
        ...


def test_localize_downloads_images_to_local(tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    monkeypatch.setattr(loc.httpx, "get", lambda *a, **k: _Resp())

    art = _article()
    localize_article(art, cfg, download_videos=False)

    assert all(u.startswith("/media/") for u in art.images)
    # files actually written under data/media/<hash>/
    for u in art.images:
        p = local_media_file(u, cfg)
        assert p is not None and p.exists()


def test_config_workers_default_and_override():
    import os
    cfg = Config(data_dir="x")
    assert cfg.workers == (os.cpu_count() or 4)
    assert Config(data_dir="x", download_workers=3).workers == 3
    # non-positive falls back to cpu count
    assert Config(data_dir="x", download_workers=0).workers == (os.cpu_count() or 4)


def test_localize_concurrent_preserves_order(tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path, download_workers=4)
    cfg.ensure_dirs()
    monkeypatch.setattr(loc.httpx, "get", lambda *a, **k: _Resp())
    art = Article(
        title="t", content_md="c", url="https://x.com/a", source_name="X",
        source_type="scrape", published_at=None,
        images=[f"https://img.cdn/{i}.png" for i in range(8)],
        raw_summary=None, fetched_at=datetime.now(timezone.utc), topic="AI",
        videos=[])
    localize_article(art, cfg, download_videos=False)
    assert len(art.images) == 8
    assert all(u.startswith("/media/") for u in art.images)
    # order preserved: img-1 maps to first url, etc.
    assert art.images[0].endswith("img-1.png")
    assert art.images[7].endswith("img-8.png")


def test_localize_keeps_remote_on_failure(tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()

    def boom(*a, **k):
        raise RuntimeError("403")
    monkeypatch.setattr(loc.httpx, "get", boom)

    art = _article()
    localize_article(art, cfg, download_videos=False)
    # download failed -> original remote URLs preserved as fallback
    assert art.images == ["https://img.cdn/a.png", "https://img.cdn/b.jpg"]


def test_save_scenes_and_resynth(tmp_path, monkeypatch):
    import json
    from app.pipeline import narration as nar
    from app.pipeline.narration import save_scenes, resynth_scenes, load_narration
    from app.tts.providers.mock import MockTTSProvider

    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    out_dir = cfg.videos_dir / "draft-7"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "script.json").write_text(json.dumps({
        "scenes": [{"narration": "old", "visual": "", "media": "none",
                    "audio": "scene-0.wav"}],
        "images": ["/media/x/img-1.png"], "videos": [],
    }), encoding="utf-8")

    # edit scenes: change narration, add a scene, set media; invalid media -> none
    edited = save_scenes("7", [
        {"narration": "new text", "visual": "v", "media": "image:1",
         "audio": "scene-0.wav"},
        {"narration": "second", "visual": "", "media": "bogus"},
        {"narration": "   "},  # empty -> dropped
    ], cfg)
    assert len(edited["scenes"]) == 2
    assert edited["scenes"][0]["media"] == "image:1"
    assert edited["scenes"][1]["media"] == "none"
    assert edited["images"] == ["/media/x/img-1.png"]  # preserved

    # resynth only scene 0
    script = resynth_scenes("7", [0], MockTTSProvider(), cfg)
    assert script["scenes"][0]["audio"].endswith(".wav")
    # persisted
    again = load_narration("7", cfg)
    assert again["scenes"][0]["audio"] == script["scenes"][0]["audio"]


def test_local_media_file_rejects_outside_paths(tmp_path):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    assert local_media_file("https://x.com/a.png", cfg) is None
    assert local_media_file("/media/../secret.txt", cfg) is None


def test_localize_one_updates_archive_frontmatter(tmp_path, monkeypatch):
    from app.db import connect, init_db
    from app.store import Store
    from app.pipeline.localize import localize_one

    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    conn = connect(cfg.db_path)
    init_db(conn)
    store = Store(conn, cfg)

    art = store.save_article(Article(
        title="Has remote",
        content_md="正文 ![](https://img.cdn/a.png) 结尾",
        url="https://x.com/a",
        source_name="X", source_type="scrape", published_at=None,
        images=["https://img.cdn/a.png"], raw_summary=None,
        fetched_at=datetime.now(timezone.utc), topic="AI"))

    monkeypatch.setattr(loc.httpx, "get", lambda *a, **k: _Resp())
    stats = localize_one(art.id, store, cfg, download_videos=False)
    assert stats["downloaded"] == 1
    assert stats["replaced"] == 1

    body = store.read_article_body(art.id)
    assert body["images"] and body["images"][0].startswith("/media/")
    # body content now references the local path, not the remote URL
    assert "https://img.cdn/a.png" not in body["content_md"]
    assert "../../media/" in body["content_md"]

    # running again downloads nothing more (already local)
    stats2 = localize_one(art.id, store, cfg, download_videos=False)
    assert stats2["downloaded"] == 0


def test_content_video_extraction():
    from app.pipeline.localize import content_videos, content_images
    body = (
        "正文 ![](https://x.com/p.png)\n"
        '<iframe src="https://www.youtube.com/embed/abc"></iframe>\n'
        "[▶ 视频](https://cdn/clip.mp4)"
    )
    assert content_images(body) == ["https://x.com/p.png"]
    vids = content_videos(body)
    assert "https://www.youtube.com/embed/abc" in vids
    assert "https://cdn/clip.mp4" in vids
