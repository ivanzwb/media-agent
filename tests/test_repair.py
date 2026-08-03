from __future__ import annotations

from datetime import datetime, timezone

from app.config import Config
from app.db import connect, init_db
from app.models import Article, Draft
from app.pipeline.repair import repair_draft_images
from app.store import Store


class _Resp:
    status_code = 200
    headers = {"content-type": "image/png"}
    content = b"\x89PNG\r\n\x1a\n" + b"0" * 64

    def raise_for_status(self):
        return None

    def iter_bytes(self, *_a, **_k):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def setup(tmp_path):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    conn = connect(config.db_path)
    init_db(conn)
    return config, Store(conn, config)


def seed(store, images):
    art = store.save_article(Article(
        title="来源一", content_md="正文", url="https://x.com/a",
        source_name="X", source_type="scrape",
        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        images=list(images), raw_summary=None,
        fetched_at=datetime(2026, 1, 2, tzinfo=timezone.utc), topic="AI"))
    return art


def draft_with(store, art, body, sources=None):
    return store.save_draft(Draft(
        article_id=art.id, title_candidates=["标题"], body_md=body,
        topic="AI", source_url=art.url, source_name=art.source_name,
        sources=sources or [{"article_id": art.id}], origin="search_create"))


def test_a_dry_run_reports_without_touching_the_draft(tmp_path):
    config, store = setup(tmp_path)
    art = seed(store, ["https://img.cdn/a.png"])
    draft = draft_with(store, art, "开头\n\n[[IMG:0]]\n\n结尾")

    repairs = repair_draft_images(store, config)

    assert len(repairs) == 1
    assert repairs[0].resolved == ["[[IMG:0]] → https://img.cdn/a.png"]
    assert repairs[0].applied is False
    assert "[[IMG:0]]" in store.read_draft_body(draft.id)["body_md"]


def test_applying_puts_the_picture_back(tmp_path, monkeypatch):
    config, store = setup(tmp_path)
    monkeypatch.setattr("app.pipeline.localize.httpx.get",
                        lambda *a, **k: _Resp())
    art = seed(store, ["https://img.cdn/a.png"])
    draft = draft_with(store, art, "开头\n\n[[IMG:0]]\n\n结尾")

    repairs = repair_draft_images(store, config, apply=True)

    body = store.read_draft_body(draft.id)["body_md"]
    assert "[[IMG:" not in body
    assert "![](/media/" in body
    assert repairs[0].applied is True


def test_a_marker_with_no_picture_behind_it_is_cleared(tmp_path):
    config, store = setup(tmp_path)
    art = seed(store, ["https://img.cdn/a.png"])
    draft = draft_with(store, art, "开头\n\n[[IMG:7]]\n\n结尾")

    repairs = repair_draft_images(store, config, apply=True)

    assert store.read_draft_body(draft.id)["body_md"] == "开头\n\n结尾"
    assert repairs[0].dropped == ["[[IMG:7]]"]


def test_an_already_localized_image_needs_no_download(tmp_path):
    """归档做过本地化后，图片已经是 /media/ 路径，直接用即可。"""
    config, store = setup(tmp_path)
    art = seed(store, ["/media/hash/a.png"])
    draft = draft_with(store, art, "开头\n\n[[IMG:0]]\n\n结尾")

    repair_draft_images(store, config, apply=True)

    body = store.read_draft_body(draft.id)["body_md"]
    assert "![](/media/hash/a.png)" in body


def test_page_furniture_is_cleared_rather_than_pasted_in(tmp_path):
    """新闻页的图片清单混着二维码和 App 角标，补回去比缺图更糟。"""
    config, store = setup(tmp_path)
    art = seed(store, ["https://www.news.cn/detail2020/images/ewm.png",
                       "https://www.news.cn/2021detail/images/qrcode-app.png",
                       "https://img.cdn/real-photo.jpg"])
    draft = draft_with(store, art, "[[IMG:0]] [[IMG:1]] [[IMG:2]]")

    repairs = repair_draft_images(store, config)

    assert repairs[0].resolved == ["[[IMG:2]] → https://img.cdn/real-photo.jpg"]
    assert len(repairs[0].skipped) == 2
    assert repairs[0].dropped == []
    assert repairs[0].markers == 3


def test_a_query_string_full_of_urls_is_not_mistaken_for_furniture(tmp_path):
    """图床把真实地址塞在 query 里，判断只看路径部分。"""
    config, store = setup(tmp_path)
    url = "https://nimg.ws.126.net/?url=http%3A%2F%2Fshare.logo%2Ficon.jpg"
    art = seed(store, [url])
    draft_with(store, art, "[[IMG:0]]")

    repairs = repair_draft_images(store, config)

    assert repairs[0].resolved == [f"[[IMG:0]] → {url}"]
    assert repairs[0].skipped == []


def test_drafts_without_markers_are_left_alone(tmp_path):
    config, store = setup(tmp_path)
    art = seed(store, ["https://img.cdn/a.png"])
    draft_with(store, art, "一篇正常的稿子，没有记号。")

    assert repair_draft_images(store, config, apply=True) == []


def _png(path, width, height):
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), "white").save(path)


def test_icons_already_pasted_into_a_draft_are_taken_back_out(tmp_path):
    """草稿 #65 的毛病：记号解析成了 66×66 的评论、分享图标。"""
    config, store = setup(tmp_path)
    _png(config.media_dir / "hash" / "photo.jpg", 1200, 800)
    _png(config.media_dir / "hash" / "icon.png", 66, 66)
    _png(config.media_dir / "hash" / "banner.png", 441, 62)
    art = seed(store, [])
    draft = draft_with(store, art, (
        "开头\n\n![](/media/hash/photo.jpg)\n\n正文\n\n"
        "![](/media/hash/icon.png)\n\n![](/media/hash/banner.png)\n\n结尾"))

    repairs = repair_draft_images(store, config, apply=True)

    body = store.read_draft_body(draft.id)["body_md"]
    assert "![](/media/hash/photo.jpg)" in body
    assert "icon.png" not in body and "banner.png" not in body
    assert repairs[0].icons == ["/media/hash/icon.png",
                                "/media/hash/banner.png"]
    assert "\n\n\n" not in body


def test_a_picture_that_is_no_longer_on_disk_is_left_where_it_is(tmp_path):
    config, store = setup(tmp_path)
    art = seed(store, [])
    draft = draft_with(store, art, "开头\n\n![](/media/hash/gone.jpg)\n\n结尾")

    repair_draft_images(store, config, apply=True)

    assert "gone.jpg" in store.read_draft_body(draft.id)["body_md"]


def test_the_marker_numbers_follow_the_order_the_sources_were_given(tmp_path):
    config, store = setup(tmp_path)
    first = seed(store, ["https://img.cdn/a.png", "https://img.cdn/b.png"])
    second = store.save_article(Article(
        title="来源二", content_md="正文", url="https://y.com/b",
        source_name="Y", source_type="scrape",
        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        images=["https://img.cdn/c.png"], raw_summary=None,
        fetched_at=datetime(2026, 1, 2, tzinfo=timezone.utc), topic="AI"))
    draft_with(store, first, "[[IMG:2]]",
               sources=[{"article_id": first.id}, {"article_id": second.id}])

    repairs = repair_draft_images(store, config)

    assert repairs[0].resolved == ["[[IMG:2]] → https://img.cdn/c.png"]
