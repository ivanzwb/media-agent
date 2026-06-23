import base64

from app.images.providers.openai_images import OpenAIImageProvider

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fakecontent"


class FakeImages:
    def generate(self, **kwargs):
        class Item:
            b64_json = base64.b64encode(PNG_BYTES).decode()

        class Resp:
            data = [Item()]
        self.kwargs = kwargs
        return Resp()


class FakeClient:
    def __init__(self, **kwargs):
        self.images = FakeImages()


def test_openai_image_writes_png(monkeypatch, tmp_path):
    monkeypatch.setattr("app.images.providers.openai_images.OpenAI", FakeClient)
    p = OpenAIImageProvider(api_key="sk-test")
    out = p.generate(prompt="a cover", out_path=tmp_path / "x.png")
    assert out.exists()
    assert out.read_bytes() == PNG_BYTES
