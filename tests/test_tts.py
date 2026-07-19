import wave

from app.tts.base import get_tts_provider, AdjustableTTS
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


def _install_fake_edge_tts(monkeypatch, captured):
    import sys
    import types

    class FakeComm:
        def __init__(self, text, voice=None, **opts):
            captured["text"] = text
            captured["voice"] = voice
            captured["opts"] = opts

        async def stream(self):
            yield {"type": "audio", "data": b"ID3fake-mp3-audio"}

    mod = types.ModuleType("edge_tts")
    mod.Communicate = FakeComm
    monkeypatch.setitem(sys.modules, "edge_tts", mod)


def test_get_tts_provider_wraps_with_adjustable_tts():
    prov = get_tts_provider("kitten", voice="female", rate="+10%", pitch="+15Hz")
    assert isinstance(prov, AdjustableTTS)
    assert prov._atempo == 1.1
    assert prov._semitones is not None  # +15Hz ≈ +2.52 semitones


def test_local_kitten_no_prosody_without_rate_pitch(monkeypatch, tmp_path):
    captured: dict = {}
    _install_fake_edge_tts(monkeypatch, captured)
    prov = get_tts_provider("kitten", voice="female")  # no rate/pitch
    out = prov.synthesize("你好世界", tmp_path / "s")
    assert out.suffix == ".mp3" and out.exists()
    # edge-tts should NOT receive rate/pitch opts anymore
    assert captured["opts"] == {} or "rate" not in captured["opts"]


def test_get_tts_provider_openai_compatible():
    from app.tts.providers.openai_compatible import OpenAICompatibleTTS
    prov = get_tts_provider("openai", base_url="http://localhost:9999/v1",
                            voice="x", model="m")
    assert isinstance(prov, OpenAICompatibleTTS)
    assert prov.base_url == "http://localhost:9999/v1"
