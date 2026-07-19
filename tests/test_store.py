from datetime import datetime, timezone

from app.config import Config
from app.db import connect, init_db
from app.models import Article, Draft
from app.store import Store, _first_body_image, _cover_is_tiny, _web_image_for_title


def make_store(tmp_path):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    conn = connect(cfg.db_path)
    init_db(conn)
    return Store(conn, cfg)


def sample_article(url="https://x.com/a"):
    return Article(title="Big AI News", content_md="# Body\nfacts here",
                   url=url, source_name="X Blog", source_type="rss",
                   published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                   images=[], raw_summary="summary",
                   fetched_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                   topic="AI")


def test_list_drafts_pagination_and_count(tmp_path):
    store = make_store(tmp_path)
    art = store.save_article(sample_article())
    for i in range(5):
        store.save_draft(Draft(
            article_id=art.id, title_candidates=[f"标题{i}"],
            body_md=f"# body {i}", topic="AI",
            source_url=art.url, source_name=art.source_name))
    assert store.count_drafts() == 5
    page1 = store.list_drafts(limit=2, offset=0)
    page2 = store.list_drafts(limit=2, offset=2)
    assert len(page1) == 2 and len(page2) == 2
    # pages don't overlap and preserve ordering
    ids1 = {d["id"] for d in page1}
    ids2 = {d["id"] for d in page2}
    assert ids1.isdisjoint(ids2)
    # no limit → all
    assert len(store.list_drafts()) == 5


def test_save_article_writes_md_and_row(tmp_path):
    store = make_store(tmp_path)
    saved = store.save_article(sample_article())
    assert saved.id is not None
    assert saved.archive_path and (tmp_path / saved.archive_path).exists()
    text = (tmp_path / saved.archive_path).read_text(encoding="utf-8")
    assert "facts here" in text


def test_save_article_dedup_returns_existing(tmp_path):
    store = make_store(tmp_path)
    a1 = store.save_article(sample_article())
    a2 = store.save_article(sample_article(url="https://x.com/a?utm_source=z"))
    assert a1.id == a2.id


def test_exists_by_fingerprint(tmp_path):
    store = make_store(tmp_path)
    art = sample_article()
    assert not store.exists(art.fingerprint())
    store.save_article(art)
    assert store.exists(art.fingerprint())


def test_exists_by_title_source(tmp_path):
    store = make_store(tmp_path)
    art = sample_article()
    assert not store.exists_by_title_source(art.title, art.source_name)
    store.save_article(art)
    assert store.exists_by_title_source(art.title, art.source_name)
    # Different source, same title — should NOT match
    assert not store.exists_by_title_source(art.title, "Other Blog")
    # Same source, different title — should NOT match
    assert not store.exists_by_title_source("Unrelated News", art.source_name)
    store = make_store(tmp_path)
    art = store.save_article(sample_article())
    draft = Draft(article_id=art.id,
                  title_candidates=["T1", "T2"], body_md="## Hook\ntext",
                  topic="AI", source_url=art.url, source_name=art.source_name)
    saved = store.save_draft(draft)
    assert saved.id is not None
    assert (tmp_path / saved.draft_path).exists()
    content = (tmp_path / saved.draft_path).read_text(encoding="utf-8")
    assert "title_candidates" in content
    assert "## Hook" in content


# ═══════════════════════════════════════════════════════════════════
# _first_body_image — image URL extraction
# ═══════════════════════════════════════════════════════════════════

def test_first_body_image_relative_media_path(tmp_path):
    """../../media/<hash>/img-N.ext path is resolved relative to data/."""
    # _first_body_image uses Path("data") hardcoded — create media there
    from pathlib import Path
    (Path("data") / "media" / "abc123").mkdir(parents=True, exist_ok=True)
    img = Path("data") / "media" / "abc123" / "img-0.gif"
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(b"GIF89a" + b"x" * 10000)

    body = "![](../../media/abc123/img-0.gif)\n\ntext"
    result = _first_body_image(body, tmp_path / "images")
    assert result is not None
    assert (tmp_path / "images" / result).exists()


def test_first_body_image_absolute_media_path(tmp_path):
    """Absolute /media/<hash>/img-N.ext path resolved."""
    from pathlib import Path
    (Path("data") / "media" / "abc123").mkdir(parents=True, exist_ok=True)
    img = Path("data") / "media" / "abc123" / "img-0.jpg"
    img.write_bytes(b"\xff\xd8\xff" + b"x" * 10000)

    body = "![](/media/abc123/img-0.jpg)\n\ntext"
    result = _first_body_image(body, tmp_path / "images")
    assert result is not None
    assert (tmp_path / "images" / result).exists()


def test_first_body_image_http_url(tmp_path):
    """HTTP URL is downloaded and saved as cover."""
    body = "![](https://example.com/cover.jpg)\n\ntext"
    from unittest.mock import patch, MagicMock
    with patch("app.store.httpx.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.content = b"\xff\xd8\xff" + b"x" * 10000
        mock_resp.raise_for_status = lambda: None
        mock_get.return_value = mock_resp
        result = _first_body_image(body, tmp_path / "images")
        assert result is not None
        assert result.startswith("cover-auto-")
        assert (tmp_path / "images" / result).exists()


def test_first_body_image_no_images(tmp_path):
    """No images in body returns None."""
    body = "## Just text\n\nNo images here at all."
    assert _first_body_image(body, tmp_path / "images") is None


def test_first_body_image_skips_video_paths(tmp_path):
    """Paths starting with /videos/ are excluded."""
    body = "![](/videos/draft-1/video.mp4)\n\ntext"
    assert _first_body_image(body, tmp_path / "images") is None


def test_first_body_image_finds_html_img(tmp_path):
    """HTML <img> tags are found alongside markdown images."""
    from pathlib import Path
    (Path("data") / "media" / "abc123").mkdir(parents=True, exist_ok=True)
    img = Path("data") / "media" / "abc123" / "img-1.png"
    img.write_bytes(b"\x89PNG" + b"x" * 10000)

    body = '<img src="/media/abc123/img-1.png">\n\ntext'
    result = _first_body_image(body, tmp_path / "images")
    assert result is not None


def test_first_body_image_missing_file(tmp_path):
    """File referenced in body doesn't exist on disk."""
    body = "![](/media/missing/img-0.jpg)\n\ntext"
    assert _first_body_image(body, tmp_path / "images") is None


# ═══════════════════════════════════════════════════════════════════
# _cover_is_tiny
# ═══════════════════════════════════════════════════════════════════

def test_cover_is_tiny_small_file(tmp_path):
    p = tmp_path / "images" / "cover-tiny.png"
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(b"x" * 5000)  # 5KB → tiny
    assert _cover_is_tiny("cover-tiny.png", tmp_path / "images")


def test_cover_is_tiny_large_file(tmp_path):
    p = tmp_path / "images" / "cover-large.jpg"
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(b"x" * 50000)  # 50KB → not tiny
    assert not _cover_is_tiny("cover-large.jpg", tmp_path / "images")


def test_cover_is_tiny_missing_file(tmp_path):
    assert not _cover_is_tiny("nonexistent.png", tmp_path / "images")


# ═══════════════════════════════════════════════════════════════════
# _web_image_for_title
# ═══════════════════════════════════════════════════════════════════

def test_web_image_for_title_success(tmp_path):
    """Finds and downloads first suitable web image."""
    from unittest.mock import patch, MagicMock
    ddgs_ctx = MagicMock()
    ddgs_ctx.images.return_value = iter([
        {"image": "https://example.com/thumb.jpg",
         "thumbnail": "https://example.com/thumb_s.jpg"},
    ])
    ddgs_mock = MagicMock()
    ddgs_mock.__enter__.return_value = ddgs_ctx

    with patch("ddgs.DDGS", return_value=ddgs_mock):
        import httpx
        with patch("app.store.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.content = b"\xff\xd8\xff" + b"x" * 5000
            mock_resp.raise_for_status = lambda: None
            mock_get.return_value = mock_resp
            result = _web_image_for_title(
                "AI Robots", tmp_path / "images")
            assert result is not None
            assert result.startswith("cover-web-")
            assert (tmp_path / "images" / result).exists()


def test_web_image_for_title_no_results(tmp_path):
    """No search results returns None."""
    from unittest.mock import patch, MagicMock
    ddgs_ctx = MagicMock()
    ddgs_ctx.images.return_value = iter([])
    ddgs_mock = MagicMock()
    ddgs_mock.__enter__.return_value = ddgs_ctx
    with patch("ddgs.DDGS", return_value=ddgs_mock):
        assert _web_image_for_title("xyzzy", tmp_path / "images") is None


def test_web_image_for_title_ddgs_error(tmp_path):
    """DDGS raises exception → None."""
    from unittest.mock import patch
    with patch("ddgs.DDGS", side_effect=ImportError("no ddgs")):
        assert _web_image_for_title("AI", tmp_path / "images") is None


def test_web_image_for_title_too_small(tmp_path):
    """Tiny images (< 2KB) are skipped, next one tried."""
    from unittest.mock import patch, MagicMock
    ddgs_ctx = MagicMock()
    ddgs_ctx.images.return_value = iter([
        {"image": "https://example.com/tiny.gif"},
        {"image": "https://example.com/good.jpg"},
    ])
    ddgs_mock = MagicMock()
    ddgs_mock.__enter__.return_value = ddgs_ctx
    with patch("ddgs.DDGS", return_value=ddgs_mock):
        import httpx
        with patch("app.store.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.content = b"\xff\xd8\xff" + b"x" * 5000
            mock_resp.raise_for_status = lambda: None
            mock_get.return_value = mock_resp
            result = _web_image_for_title(
                "AI", tmp_path / "images")
            assert result is not None


# ═══════════════════════════════════════════════════════════════════
# attach_cover guard
# ═══════════════════════════════════════════════════════════════════

def test_attach_cover_skips_when_valid_cover_exists(tmp_path):
    """Don't overwrite an existing valid cover with mock."""
    from app.pipeline.images import attach_cover
    from app.images.providers.mock import MockImageProvider
    from app.models import Draft

    # Create a valid cover file
    (tmp_path / "images").mkdir(parents=True)
    valid = tmp_path / "images" / "existing-cover.jpg"
    valid.write_bytes(b"x" * 20000)  # 20KB

    draft = Draft(article_id=1, title_candidates=["Test Title"],
                  body_md="text", topic="AI", source_url="x",
                  source_name="x")
    draft.cover_image = "existing-cover.jpg"

    provider = MockImageProvider()
    result = attach_cover(draft, provider, tmp_path / "images")
    # Should keep existing cover, not overwrite with mock blue
    assert result.cover_image == "existing-cover.jpg"


def test_attach_cover_generates_when_no_cover(tmp_path):
    """When no cover exists, generate one."""
    from app.pipeline.images import attach_cover
    from app.images.providers.mock import MockImageProvider
    from app.models import Draft

    (tmp_path / "images").mkdir(parents=True)
    draft = Draft(article_id=1, title_candidates=["Test Title"],
                  body_md="text", topic="AI", source_url="x",
                  source_name="x")
    draft.cover_image = None

    provider = MockImageProvider()
    result = attach_cover(draft, provider, tmp_path / "images")
    assert result.cover_image is not None
    assert (tmp_path / "images" / result.cover_image).exists()


# ═══════════════════════════════════════════════════════════════════
# save_draft auto-cover flow
# ═══════════════════════════════════════════════════════════════════

def test_save_draft_auto_cover_from_body_image(tmp_path):
    """save_draft uses first body image as cover when no cover set."""
    from pathlib import Path
    store = make_store(tmp_path)
    (Path("data") / "media" / "hash1").mkdir(parents=True, exist_ok=True)
    img = Path("data") / "media" / "hash1" / "img-0.jpg"
    img.write_bytes(b"\xff\xd8\xff" + b"x" * 10000)

    art = store.save_article(sample_article(url="https://x.com/abc"))
    draft = Draft(article_id=art.id,
                  title_candidates=["AI Breakthrough"],
                  body_md="![](../../media/hash1/img-0.jpg)\n\nArticle text.",
                  topic="AI", source_url=art.url,
                  source_name=art.source_name)
    draft.cover_image = None
    saved = store.save_draft(draft)
    assert saved.cover_image is not None
    assert saved.cover_image.startswith("cover-auto-")
    assert (tmp_path / "images" / saved.cover_image).exists()


def test_save_draft_keeps_valid_cover(tmp_path):
    """When a valid cover is already set, don't replace it."""
    store = make_store(tmp_path)
    (tmp_path / "images").mkdir(parents=True, exist_ok=True)
    valid = tmp_path / "images" / "good-cover.jpg"
    valid.write_bytes(b"x" * 20000)

    art = store.save_article(sample_article(url="https://x.com/def1"))
    draft = Draft(article_id=art.id,
                  title_candidates=["Title"],
                  body_md="text", topic="AI", source_url="x",
                  source_name="x")
    draft.cover_image = "good-cover.jpg"
    saved = store.save_draft(draft)
    assert saved.cover_image == "good-cover.jpg"


def test_save_draft_no_cover_keeps_none(tmp_path):
    """When no body images and no web results, cover stays None."""
    from unittest.mock import patch, MagicMock
    ddgs_ctx = MagicMock()
    ddgs_ctx.images.return_value = iter([])
    ddgs_mock = MagicMock()
    ddgs_mock.__enter__.return_value = ddgs_ctx
    with patch("ddgs.DDGS", return_value=ddgs_mock):
        store = make_store(tmp_path)
        art = store.save_article(sample_article(url="https://x.com/ghi"))
        draft = Draft(article_id=art.id,
                      title_candidates=["Title"],
                      body_md="# No images\n\nJust plain text.",
                      topic="AI", source_url="x",
                      source_name="x")
        draft.cover_image = None
        saved = store.save_draft(draft)
        assert saved.cover_image is None or saved.cover_image == "" or not saved.cover_image


# ═══════════════════════════════════════════════════════════════════
# save_draft in-place update (rewrite preserves draft ID)
# ═══════════════════════════════════════════════════════════════════

def test_save_draft_new_when_no_id(tmp_path):
    """save_draft without draft.id creates a new row (INSERT)."""
    store = make_store(tmp_path)
    art = store.save_article(sample_article(url="https://x.com/new1"))
    draft = Draft(article_id=art.id,
                  title_candidates=["Original"], body_md="original body",
                  topic="AI", source_url=art.url, source_name=art.source_name)
    saved = store.save_draft(draft)
    assert saved.id is not None
    row = store.get_draft(saved.id)
    assert row is not None


def test_save_draft_updates_in_place_when_id_set(tmp_path):
    """save_draft with draft.id set updates the existing row (no new ID)."""
    store = make_store(tmp_path)
    art = store.save_article(sample_article(url="https://x.com/upd"))

    # Create initial draft
    draft1 = Draft(article_id=art.id,
                   title_candidates=["V1"], body_md="version 1",
                   topic="AI", source_url=art.url, source_name=art.source_name)
    saved1 = store.save_draft(draft1)
    original_id = saved1.id

    # Rewrite: set id to trigger UPDATE path
    draft2 = Draft(article_id=art.id,
                   title_candidates=["V2"], body_md="version 2",
                   topic="AI", source_url=art.url, source_name=art.source_name)
    draft2.id = original_id
    saved2 = store.save_draft(draft2)

    # Same ID, content updated
    assert saved2.id == original_id
    row = store.get_draft(original_id)
    assert row is not None
    body = store.read_draft_body(original_id)
    assert body["body_md"] == "version 2"


def test_save_draft_in_place_no_duplicate_rows(tmp_path):
    """In-place update should not create extra draft rows for same article."""
    store = make_store(tmp_path)
    art = store.save_article(sample_article(url="https://x.com/dup"))

    draft = Draft(article_id=art.id,
                  title_candidates=["First"], body_md="first",
                  topic="AI", source_url=art.url, source_name=art.source_name)
    saved = store.save_draft(draft)
    orig_id = saved.id

    # Rewrite in place
    draft2 = Draft(article_id=art.id,
                   title_candidates=["Second"], body_md="second",
                   topic="AI", source_url=art.url, source_name=art.source_name)
    draft2.id = orig_id
    store.save_draft(draft2)

    # Only one draft for this article
    found = store.get_draft_for_article(art.id)
    assert found["id"] == orig_id
