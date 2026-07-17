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


class PickyImages:
    """Rejects response_format (like a litellm proxy / non-DALL·E model),
    succeeds once it's dropped."""
    def __init__(self):
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        if "response_format" in kwargs:
            raise Exception(
                "UnsupportedParamsError: Setting `response_format` is not "
                "supported by openai. To drop it set litellm.drop_params = True")

        class Item:
            b64_json = base64.b64encode(PNG_BYTES).decode()

        class Resp:
            data = [Item()]
        return Resp()


def test_drops_unsupported_response_format(monkeypatch, tmp_path):
    picky = PickyImages()

    class Client:
        def __init__(self, **kwargs):
            self.images = picky

    monkeypatch.setattr("app.images.providers.openai_images.OpenAI", Client)
    p = OpenAIImageProvider(api_key="sk-test", model="agnes-text-to-image-model")
    out = p.generate(prompt="a cover", out_path=tmp_path / "y.png")
    assert out.read_bytes() == PNG_BYTES
    # retried without response_format
    assert any("response_format" not in c for c in picky.calls)


def test_url_response_is_downloaded(monkeypatch, tmp_path):
    class UrlImages:
        def generate(self, **kwargs):
            class Item:
                url = "https://img.example/cover.png"

            class Resp:
                data = [Item()]
            return Resp()

    class Client:
        def __init__(self, **kwargs):
            self.images = UrlImages()

    monkeypatch.setattr("app.images.providers.openai_images.OpenAI", Client)
    import httpx

    class FakeResp:
        content = PNG_BYTES

        def raise_for_status(self):
            pass

    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResp())
    p = OpenAIImageProvider(api_key="sk-test")
    out = p.generate(prompt="a cover", out_path=tmp_path / "z.png")
    assert out.read_bytes() == PNG_BYTES
