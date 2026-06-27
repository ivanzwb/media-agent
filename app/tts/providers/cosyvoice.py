from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import numpy as np

# CosyVoice2 — local zero-shot voice cloning TTS.
# Best-quality Chinese TTS with voice cloning from a 3–10 s reference sample.
# Requires PyTorch + CUDA (4 GB+ VRAM) and Python < 3.13.
#
# Install:
#   pip install cosyvoice
#   huggingface-cli download FunAudioLLM/CosyVoice2-0.5B \
#     --local-dir pretrained_models/CosyVoice2-0.5B
#
# The model is loaded lazily and cached process-wide (first call is slow,
# ~10–30 s to load weights; subsequent calls run on GPU).

logger = logging.getLogger(__name__)

_MODEL = None
_WHISPER = None
_DEFAULT_MODEL = "FunAudioLLM/CosyVoice2-0.5B"


class CosyVoiceTTS:
    """Local voice cloning via CosyVoice2 (FunAudioLLM).

    ``voice`` = local voice-registry ID (same as xtts used).  The caller
    resolves it to a reference sample path.  Each ``synthesize()`` call
    runs zero-shot voice cloning via ``inference_zero_shot()``.

    When ``model`` is a local directory path it is used directly; otherwise
    it is treated as a HuggingFace model ID and auto-downloaded (requires
    ``huggingface_hub``).
    """

    def __init__(self, speaker_wav: str | None = None,
                 model: str | None = None):
        self.speaker_wav = speaker_wav
        self.model_name = model or _DEFAULT_MODEL
        self._model_dir: str | None = None  # resolved local path
        self._spk_id: str | None = None     # set after first add_zero_shot_spk
        self._prompt_text: str = ""          # whisper transcription of ref audio

    # ------------------------------------------------------------------
    # Model loading (lazy, process-global singleton)
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_model_path(model_name: str) -> str:
        """Return a local directory path for *model_name*.

        - If *model_name* exists as a local path → use it directly.
        - Otherwise check well-known local locations.
        - If still not found, attempt to download from HuggingFace Hub.
        """
        p = Path(model_name)
        if p.exists():
            logger.info("使用本地模型路径 %s", p)
            return str(p.resolve())

        # Check well-known local paths before attempting HF download
        known_paths = [
            Path.cwd() / "pretrained_models" / "CosyVoice2-0.5B",
            Path.cwd() / "pretrained_models" / "CosyVoice2-0.5B" / "CosyVoice2-0.5B",
            Path.home() / "CosyVoice" / "pretrained_models" / "CosyVoice2-0.5B",
            # CosyVoice repo cloned alongside media-agent
            Path.cwd().parent / "CosyVoice" / "pretrained_models" / "CosyVoice2-0.5B",
            # Embedded Python setup path
            Path(r"C:\Projects\CosyVoice\pretrained_models\CosyVoice2-0.5B"),
        ]
        for path in known_paths:
            if path.exists():
                logger.info("在 %s 找到本地模型", path)
                return str(path.resolve())

        # Try auto-download from HuggingFace
        try:
            from huggingface_hub import snapshot_download
        except ImportError:
            raise RuntimeError(
                f"模型路径 '{model_name}' 不存在，且 huggingface_hub 未安装。\n"
                f"请手动下载模型：\n"
                f"  huggingface-cli download {model_name} "
                f"--local-dir pretrained_models/{model_name.split('/')[-1]}\n"
                f"或将 MEDIA_AGENT_TTS_MODEL 设为模型所在目录"
            ) from None

        logger.info("正在从 HuggingFace 下载模型 %s …", model_name)
        try:
            return snapshot_download(
                model_name,
                local_dir=None,  # use HF cache
                resume_download=True,
            )
        except Exception as e:
            raise RuntimeError(
                f"自动下载模型失败：{e}\n\n"
                f"请手动下载模型，或将 MEDIA_AGENT_TTS_MODEL 设为模型所在目录：\n"
                f"  huggingface-cli download {model_name} "
                f"--local-dir pretrained_models/{model_name.split('/')[-1]}\n"
            ) from e

    def _get_model(self):
        global _MODEL
        if _MODEL is None:
            try:
                # CosyVoice2 is the latest model supporting zero-shot cloning
                from cosyvoice.cli.cosyvoice import CosyVoice2
            except ImportError as e:
                raise RuntimeError(
                    "未安装 CosyVoice：请 `pip install cosyvoice`，"
                    "然后下载模型：\n"
                    "  huggingface-cli download FunAudioLLM/CosyVoice2-0.5B "
                    "--local-dir pretrained_models/CosyVoice2-0.5B\n\n"
                    "注意：CosyVoice2 需要 Python < 3.13 且建议有 NVIDIA GPU。"
                ) from e

            if self._model_dir is None:
                self._model_dir = self._resolve_model_path(self.model_name)

            import torch
            fp16 = torch.cuda.is_available()
            logger.info(
                "加载 CosyVoice2 模型 %s (fp16=%s) …", self._model_dir, fp16)
            _MODEL = CosyVoice2(
                self._model_dir,
                load_jit=False,
                load_trt=False,
                fp16=fp16,
            )
            logger.info("CosyVoice2 模型加载完成")
        return _MODEL

    @staticmethod
    def _get_whisper():
        """Lazy-loaded process-global whisper model for transcribing reference audio."""
        global _WHISPER
        if _WHISPER is None:
            import whisper
            logger.info("加载 Whisper tiny 模型（用于参考音频转写）…")
            _WHISPER = whisper.load_model("tiny")
            logger.info("Whisper 模型加载完成")
        return _WHISPER

    @staticmethod
    def _transcribe_ref_audio(wav_path: str) -> str:
        """Transcribe reference audio with whisper to get prompt_text.

        This is critical for CosyVoice zero-shot TTS — the LLM needs
        ``prompt_text`` (the transcript of reference speech) to properly
        align the reference speech tokens during inference.  Without it
        the generated audio is garbled/stretched.

        Returns the transcribed text (best guess), or empty string on failure.
        """
        try:
            # whisper works best with 16kHz audio
            model = CosyVoiceTTS._get_whisper()
            result = model.transcribe(wav_path, language="zh", task="transcribe",
                                      verbose=False, fp16=True)
            text = result.get("text", "").strip()
            logger.info("参考音频转写结果: 「%s」", text)
            return text
        except Exception as e:
            logger.warning("参考音频转写失败: %s", e)
            return ""

    # ------------------------------------------------------------------
    # TTSProvider interface
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_ref_wav(speaker_wav: str) -> str:
        """Return a 16kHz/16bit WAV path for a speaker reference audio.

        - If the reference is **already a WAV file** → use it as is.
        - If a pre-converted ``{stem}_16k.wav`` exists alongside → reuse it.
        - Otherwise → convert the non-WAV file (e.g. browser-recorded
          ``.webm``) once and cache the result as ``{stem}_16k.wav``.

        This avoids re-converting the same reference audio on every
        ``synthesize()`` call.
        """
        p = Path(speaker_wav).resolve()
        if p.suffix.lower() == ".wav":
            return str(p)

        cached = p.with_name(f"{p.stem}_16k.wav")
        if cached.exists():
            logger.info("使用缓存的参考音频 %s", cached)
            return str(cached)

        try:
            from pydub import AudioSegment
            audio = AudioSegment.from_file(str(p))
            # CosyVoice 期望 16-bit 16kHz PCM WAV；浏览器录制的 webm 通常为
            # 48 kHz 32-bit float，不匹配会导致音色克隆失真。
            audio = audio.set_frame_rate(16000).set_sample_width(2)
            audio.export(str(cached), format="wav")
            logger.info("参考音频 %s → 缓存为 16kHz/16bit WAV %s",
                        p, cached)
            return str(cached)
        except Exception as e:
            raise RuntimeError(
                f"参考音频格式转换失败（{speaker_wav}）：{e}") from e

    def synthesize(self, text: str, out_stem: Path) -> Path:
        if not self.speaker_wav or not Path(self.speaker_wav).exists():
            raise RuntimeError(
                "未选择声音样本：请在「设置 → 声音库」录入并选用一个声音")

        path = Path(out_stem).with_suffix(".wav")
        path.parent.mkdir(parents=True, exist_ok=True)

        import soundfile as sf

        model = self._get_model()
        prompt_wav = self._resolve_ref_wav(self.speaker_wav)

        # --- Speaker embedding caching ---
        #
        # CosyVoice's inference_zero_shot() calls frontend_zero_shot() which
        # re-processes the reference audio EVERY invocation — 3 expensive
        # operations:
        #   1) _extract_speech_feat()  — audio feature extraction
        #   2) _extract_speech_token() — ONNX speech tokenizer (CUDA)
        #   3) _extract_spk_embedding() — ONNX campplus speaker embed (CPU)
        #
        # We use add_zero_shot_spk() to run these ONCE and cache in
        # model.frontend.spk2info.  Subsequent calls pass zero_shot_spk_id
        # so frontend_zero_shot() skips redundant processing entirely.
        #
        if self._spk_id is None:
            self._spk_id = str(id(self.speaker_wav))
            # --- Transcribe reference audio for proper prompt_text ---
            #
            # CosyVoice's zero-shot LLM needs prompt_text (transcript of the
            # reference speech) to align reference speech tokens.  Without it
            # the LLM generates garbled tokens → stretched/gibberish audio.
            #
            self._prompt_text = self._transcribe_ref_audio(prompt_wav)
            if self._prompt_text:
                logger.info("参考音频转写完成（%d 字），注册说话人…", len(self._prompt_text))
                model.add_zero_shot_spk(self._prompt_text, prompt_wav, self._spk_id)
            else:
                # Fallback: register with empty text — will work but quality
                # will be poor (the underlying issue we're fixing).
                logger.warning("参考音频转写失败，使用空文本注册（音质可能受损）")
                model.add_zero_shot_spk("", prompt_wav, self._spk_id)
            logger.info("声音特征已注册（spk_id=%s），后续分镜跳过参考音频处理",
                        self._spk_id)

        for result in model.inference_zero_shot(
            tts_text=text,
            prompt_text=self._prompt_text,
            prompt_wav=prompt_wav,
            zero_shot_spk_id=self._spk_id,
            stream=False,
        ):
            audio = result["tts_speech"]  # shape: [1, samples]
            sf.write(str(path), audio.squeeze(0).numpy(), model.sample_rate)

        if not path.exists():
            raise RuntimeError(
                "CosyVoice2 推理未产生输出文件")
        return path
