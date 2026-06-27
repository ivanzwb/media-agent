from __future__ import annotations

from pathlib import Path

# Local, high-quality voice cloning via Coqui XTTS-v2. Few-shot: clones a voice
# from a short reference sample (speaker_wav), natural prosody, supports Chinese.
# Heavy optional dependency — install with: pip install TTS
# The model is loaded lazily and cached process-wide (first call is slow).

_MODEL = None
_DEFAULT_MODEL = "tts_models/multilingual/multi-dataset/xtts_v2"


class XttsTTSProvider:
    def __init__(self, speaker_wav: str | None = None,
                 language: str | None = None, model_name: str | None = None):
        self.speaker_wav = speaker_wav
        self.language = (language or "zh").strip() or "zh"
        self.model_name = model_name or _DEFAULT_MODEL

    def _get_model(self):
        global _MODEL
        if _MODEL is None:
            try:
                from TTS.api import TTS  # heavy optional dependency
            except ImportError as e:
                raise RuntimeError(
                    "未安装本地声音克隆引擎 XTTS：请 `pip install TTS`（Coqui）"
                ) from e
            _MODEL = TTS(self.model_name)
        return _MODEL

    def synthesize(self, text: str, out_stem: Path) -> Path:
        if not self.speaker_wav or not Path(self.speaker_wav).exists():
            raise RuntimeError(
                "未选择声音样本：请在「设置 → 声音库」录入并选用一个声音")
        path = Path(out_stem).with_suffix(".wav")
        path.parent.mkdir(parents=True, exist_ok=True)
        model = self._get_model()
        model.tts_to_file(text=text, speaker_wav=str(self.speaker_wav),
                          language=self.language, file_path=str(path))
        return path
