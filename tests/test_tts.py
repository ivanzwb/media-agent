import wave

from app.tts.base import get_tts_provider
from app.tts.providers.mock import MockTTSProvider
from app.tts.providers.local_kitten import LocalKittenTTS


def test_get_tts_provider_defaults_to_local_kitten():
    # Empty/None/"kitten" -> in-process Kitten (edge-tts); mock is explicit.
    assert isinstance(get_tts_provider(None), LocalKittenTTS)
    assert isinstance(get_tts_provider(""), LocalKittenTTS)
    assert isinstance(get_tts_provider("kitten"), LocalKittenTTS)
    assert isinstance(get_tts_provider("mock"), MockTTSProvider)


def test_local_kitten_resolves_voice():
    prov = get_tts_provider("kitten", voice="female")
    assert prov._resolve_voice(True) == "zh-CN-XiaoxiaoNeural"   # Chinese
    assert prov._resolve_voice(False) == "en-US-JennyNeural"     # English
    # concrete edge voice passes through; alias resolves
    assert get_tts_provider("kitten", voice="zh-CN-YunyangNeural") \
        ._resolve_voice(True) == "zh-CN-YunyangNeural"
    assert get_tts_provider("kitten", voice="男声")._resolve_voice(True) \
        == "zh-CN-YunjianNeural"


def test_mock_tts_writes_wav_scaling_with_text(tmp_path):
    prov = MockTTSProvider()
    short = prov.synthesize("短", tmp_path / "a")
    long = prov.synthesize("这是一段明显更长的口播旁白文字内容用于测试时长", tmp_path / "b")
    assert short.suffix == ".wav" and short.exists()
    with wave.open(str(short)) as w:
        short_frames = w.getnframes()
    with wave.open(str(long)) as w:
        long_frames = w.getnframes()
    assert long_frames > short_frames


def test_get_tts_provider_openai_compatible():
    from app.tts.providers.openai_compatible import OpenAICompatibleTTS
    prov = get_tts_provider("openai", base_url="http://localhost:9999/v1",
                            voice="x", model="m")
    assert isinstance(prov, OpenAICompatibleTTS)
    assert prov.base_url == "http://localhost:9999/v1"


def test_kitten_provider_normalizes_base_and_payload(monkeypatch, tmp_path):
    from app.tts.providers import kitten as kitten_mod
    from app.tts.providers.kitten import KittenTTSProvider

    # /v1 suffix should be stripped; endpoint adds /v1/tts/kitten back.
    prov = get_tts_provider("kitten_http", base_url="http://127.0.0.1:3003/v1",
                            voice="female")
    assert isinstance(prov, KittenTTSProvider)
    assert prov.base_url == "http://127.0.0.1:3003"

    captured = {}

    class FakeResp:
        content = b"RIFFfake-wav-bytes"

        def raise_for_status(self):
            ...

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return FakeResp()

    monkeypatch.setattr(kitten_mod.httpx, "post", fake_post)
    out = prov.synthesize("你好", tmp_path / "scene-0")
    assert captured["url"] == "http://127.0.0.1:3003/v1/tts/kitten"
    assert captured["json"]["profile"] == "female"  # known profile
    assert captured["json"]["format"] == "wav"
    assert out.suffix == ".wav" and out.read_bytes().startswith(b"RIFF")


def test_kitten_provider_treats_unknown_voice_as_voice_name(monkeypatch, tmp_path):
    from app.tts.providers import kitten as kitten_mod
    prov = get_tts_provider("kitten_http", base_url="http://127.0.0.1:3003",
                            voice="Luna")
    captured = {}

    class FakeResp:
        content = b"RIFF"

        def raise_for_status(self):
            ...

    monkeypatch.setattr(kitten_mod.httpx, "post",
                        lambda url, json=None, timeout=None: captured.update(json=json) or FakeResp())
    prov.synthesize("hi", tmp_path / "s")
    assert captured["json"].get("voice") == "Luna"
    assert "profile" not in captured["json"]
