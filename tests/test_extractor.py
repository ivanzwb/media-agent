from pathlib import Path

from app.sources.extractor import extract_from_html

FIXTURE = Path(__file__).parent / "fixtures" / "sample_article.html"


def test_extract_returns_title_and_body():
    html = FIXTURE.read_text(encoding="utf-8")
    result = extract_from_html(html, url="https://example.com/post")
    assert "Physical AI" in result["title"]
    assert "world model" in result["content_md"]
    assert "junk" not in result["content_md"]


def test_extract_collects_images():
    html = FIXTURE.read_text(encoding="utf-8")
    result = extract_from_html(html, url="https://example.com/post")
    assert any("figure1.png" in img for img in result["images"])
