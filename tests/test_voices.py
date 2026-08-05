import shutil
import subprocess
import wave

import pytest

from app.config import Config
from app.tts import voices as voices_mod
from app.tts.voices import (
    add_voice, list_voices, get_voice, delete_voice, sample_path,
    reference_wav)
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


def _webm_voice(tmp_path):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    # A real WebM/Opus clip, the shape a browser recording arrives in.
    src = tmp_path / "rec.webm"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", "libopus", str(src)],
        capture_output=True, check=True, timeout=120)
    return cfg, add_voice(cfg, "录音", src.read_bytes(), "recording.webm")


def test_a_wav_sample_is_handed_over_untouched(tmp_path):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    v = add_voice(cfg, "已是wav", b"RIFFfake-wav", "ref.wav")
    assert reference_wav(cfg, v["id"]) == sample_path(cfg, v["id"])


def test_reference_of_a_missing_voice_is_none(tmp_path):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    assert reference_wav(cfg, None) is None
    assert reference_wav(cfg, "does-not-exist") is None


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="需要 ffmpeg")
def test_a_browser_recording_is_converted_to_readable_wav(tmp_path):
    """WebM is what the recorder produces and what libsndfile refuses to open."""
    cfg, v = _webm_voice(tmp_path)
    with pytest.raises(wave.Error):         # the raw sample: "Format not recognised"
        wave.open(str(sample_path(cfg, v["id"])))
    ref = reference_wav(cfg, v["id"])
    assert ref is not None and ref.suffix == ".wav"
    with wave.open(str(ref)) as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getnframes() > 0


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="需要 ffmpeg")
def test_the_conversion_is_cached_not_repeated(tmp_path):
    cfg, v = _webm_voice(tmp_path)
    first = reference_wav(cfg, v["id"])
    stamp = first.stat().st_mtime_ns
    assert reference_wav(cfg, v["id"]) == first
    assert first.stat().st_mtime_ns == stamp


def test_without_ffmpeg_the_error_names_the_cure(tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    monkeypatch.setattr(voices_mod.shutil, "which", lambda _: None)
    v = add_voice(cfg, "录音", b"not-really-webm", "recording.webm")
    with pytest.raises(RuntimeError, match="ffmpeg"):
        reference_wav(cfg, v["id"])


def test_get_tts_provider_cosyvoice(tmp_path):
    from app.tts.providers.cosyvoice_runtime import CosyVoiceRuntimeTTS
    prov = get_tts_provider("cosyvoice", voice="/tmp/ref.wav",
                            model="ignored", data_dir=tmp_path)
    assert isinstance(prov, CosyVoiceRuntimeTTS)
    assert prov.speaker_wav == "/tmp/ref.wav"
    assert prov.data_dir == tmp_path


def test_cosyvoice_requires_sample(tmp_path):
    import pytest
    from app.tts.providers.cosyvoice_runtime import CosyVoiceRuntimeTTS
    prov = CosyVoiceRuntimeTTS(data_dir=tmp_path, speaker_wav=None)
    with pytest.raises(RuntimeError):
        prov.synthesize("你好", tmp_path / "out")
