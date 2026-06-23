import json
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


def sample_article(url="https://x.com/a", topic="AI", title="Big AI News"):
    return Article(title=title, content_md="# Body\nfacts here",
                   url=url, source_name="X Blog", source_type="rss",
                   published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                   images=[], raw_summary="summary",
                   fetched_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                   topic=topic)


def seed_draft(store, art):
    draft = Draft(article_id=art.id, platform="master",
                  title_candidates=["T1", "T2"], body_md="## Hook\ntext",
                  topic="AI", source_url=art.url, source_name=art.source_name)
    return store.save_draft(draft)


def test_list_and_get_article(tmp_path):
    store = make_store(tmp_path)
    store.save_article(sample_article(url="https://x.com/a", topic="AI"))
    store.save_article(sample_article(url="https://x.com/b", topic="物理AI",
                                      title="Robot"))
    assert len(store.list_articles()) == 2
    assert len(store.list_articles(topic="AI")) == 1
    art = store.list_articles(topic="AI")[0]
    assert store.get_article(art["id"])["topic"] == "AI"


def test_list_topics(tmp_path):
    store = make_store(tmp_path)
    store.save_article(sample_article(url="https://x.com/a", topic="AI"))
    store.save_article(sample_article(url="https://x.com/b", topic="物理AI",
                                      title="R"))
    assert set(store.list_topics()) == {"AI", "物理AI"}


def test_draft_read_update_roundtrip(tmp_path):
    store = make_store(tmp_path)
    art = store.save_article(sample_article())
    draft = seed_draft(store, art)
    body = store.read_draft_body(draft.id)
    assert body["body_md"].strip().startswith("## Hook")
    assert body["title_candidates"] == ["T1", "T2"]

    store.update_draft_body(draft.id, ["新标题"], "## 新正文\n改好了",
                            status="approved")
    reloaded = store.read_draft_body(draft.id)
    assert reloaded["title_candidates"] == ["新标题"]
    assert "新正文" in reloaded["body_md"]
    assert store.get_draft(draft.id)["status"] == "approved"


def test_set_draft_cover(tmp_path):
    store = make_store(tmp_path)
    art = store.save_article(sample_article())
    draft = seed_draft(store, art)
    store.set_draft_cover(draft.id, "cover-x.png")
    assert store.get_draft(draft.id)["cover_image"] == "cover-x.png"
    assert store.read_draft_body(draft.id)["cover_image"] == "cover-x.png"


def test_settings_get_set(tmp_path):
    store = make_store(tmp_path)
    assert store.get_setting("schedule_cron", "none") == "none"
    store.set_setting("schedule_cron", "0 8 * * *")
    assert store.get_setting("schedule_cron") == "0 8 * * *"
    store.set_setting("schedule_cron", "0 9 * * *")
    assert store.get_setting("schedule_cron") == "0 9 * * *"


def test_runs_and_dashboard_stats(tmp_path):
    store = make_store(tmp_path)
    art = store.save_article(sample_article())
    seed_draft(store, art)
    run_id = store.record_run()
    store.finish_run(run_id, json.dumps({"fetched": 1}), status="done")
    runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "done"
    stats = store.dashboard_stats()
    assert stats == {"articles": 1, "drafts": 1, "runs": 1}
