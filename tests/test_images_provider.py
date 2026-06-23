from pathlib import Path

import pytest

from app.images.base import get_image_provider
from app.images.providers.mock import MockImageProvider


def test_get_image_provider_mock():
    assert isinstance(get_image_provider("mock"), MockImageProvider)


def test_get_image_provider_default_is_mock():
    assert isinstance(get_image_provider(None), MockImageProvider)


def test_mock_generates_png(tmp_path):
    p = get_image_provider("mock")
    out = p.generate(prompt="cover for AI article", out_path=tmp_path / "c.png")
    assert Path(out).exists()
    assert Path(out).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_get_image_provider_unknown_raises():
    with pytest.raises(ValueError):
        get_image_provider("nope")
