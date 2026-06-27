from app.config import Config
from app.tts.voices import (
    add_voice, list_voices, get_voice, delete_voice, sample_path)
from app.tts.base import get_tts_provider


def test_voice_crud(tmp_path):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()

    assert list_voices(cfg) == []
    v = add_voice(cfg, "张三-播音腔", b"RIFFfake-wav", "ref.wav")
    assert v["id"] and v["name"] == "张三-播音腔"
    assert v["sample"].endswith(".wav")

    voices = list_voices(cfg)
    assert len(voices) == 1
    assert get_voice(cfg, v["id"])["name"] == "张三-播音腔"

    # sample resolves to a real file on disk
    p = sample_path(cfg, v["id"])
    assert p is not None and p.exists() and p.read_bytes() == b"RIFFfake-wav"

    # delete removes registry entry + file
    assert delete_voice(cfg, v["id"]) is True
    assert list_voices(cfg) == []
    assert sample_path(cfg, v["id"]) is None
    assert delete_voice(cfg, v["id"]) is False  # already gone


def test_sample_path_rejects_unknown_and_traversal(tmp_path):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    assert sample_path(cfg, None) is None
    assert sample_path(cfg, "does-not-exist") is None


def test_get_tts_provider_xtts():
    from app.tts.providers.xtts import XttsTTSProvider
    prov = get_tts_provider("xtts", voice="/tmp/ref.wav", model="zh")
    assert isinstance(prov, XttsTTSProvider)
    assert prov.speaker_wav == "/tmp/ref.wav"
    assert prov.language == "zh"


def test_xtts_requires_sample(tmp_path):
    import pytest
    from app.tts.providers.xtts import XttsTTSProvider
    prov = XttsTTSProvider(speaker_wav=None, language="zh")
    with pytest.raises(RuntimeError):
        prov.synthesize("你好", tmp_path / "out")
