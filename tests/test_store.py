from datetime import datetime, timezone

from app.config import Config
from app.db import connect, init_db
from app.models import Article, Draft
from app.store import Store


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


def test_save_draft_writes_md(tmp_path):
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
