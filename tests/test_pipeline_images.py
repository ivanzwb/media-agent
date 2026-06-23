from app.models import Draft
from app.images.providers.mock import MockImageProvider
from app.pipeline.images import download_original_images, attach_cover


def test_download_original_images_writes_files(tmp_path):
    urls = ["https://x.com/a.png", "https://x.com/b.jpg", "https://x.com/bad.png"]

    def fake(url):
        if "bad" in url:
            raise RuntimeError("fail")
        return b"\x89PNG\r\n\x1a\n" + b"data"

    saved = download_original_images(urls, tmp_path, fetch=fake)
    assert len(saved) == 2
    assert (tmp_path / saved[0]).exists()
    assert saved[1].endswith(".jpg")


def test_download_skips_empty(tmp_path):
    saved = download_original_images(["https://x.com/a.png"], tmp_path,
                                    fetch=lambda u: b"")
    assert saved == []


def test_attach_cover_sets_path_and_writes(tmp_path):
    draft = Draft(article_id=1, platform="master",
                  title_candidates=["震撼AI新闻"], body_md="b", topic="AI",
                  source_url="https://x.com/a", source_name="X")
    out = attach_cover(draft, MockImageProvider(), tmp_path)
    assert out.cover_image is not None
    assert (tmp_path / out.cover_image).exists()
