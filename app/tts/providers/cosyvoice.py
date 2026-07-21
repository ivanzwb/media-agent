from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
from pathlib import Path

# NOTE: heavy deps (numpy/torch/cosyvoice) are imported *after*
# _discover_sidecar_python() below, so a sidecar install (setup-optional) or a
# venv install both satisfy them. Importing numpy here (before the sidecar path
# is added) is what caused "No module named 'numpy'" even with the sidecar set up.

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
_MODEL_LOCK = threading.Lock()
_WHISPER_LOCK = threading.Lock()
_INFERENCE_LOCK = threading.Lock()
_PROMPT_LOCK = threading.Lock()  # serialize _prompt_text init across threads
_DEFAULT_MODEL = "FunAudioLLM/CosyVoice2-0.5B"


# ------------------------------------------------------------------
# Sidecar embedded Python discovery
# When CosyVoice is installed via setup-optional.bat/sh, PyTorch and
# cosyvoice live inside _cosyvoice-python/ (an embedded/portable Python).
# We add its site-packages to sys.path so imports work transparently.
# ------------------------------------------------------------------
def _discover_sidecar_python():
    """Add the sidecar Python's site-packages to sys.path if present."""
    # Determine bundle root: PyInstaller frozen → _MEIPASS/..  else CWD
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        bundle_root = os.path.dirname(sys._MEIPASS)
    else:
        bundle_root = os.getcwd()

    sidecar_site = os.path.join(
        bundle_root, '_cosyvoice-python', 'Lib', 'site-packages')
    if os.path.isdir(sidecar_site) and sidecar_site not in sys.path:
        sys.path.insert(0, sidecar_site)
        # Also check for python<version>/site-packages (macOS layout)
        sidecar_site2 = os.path.join(
            bundle_root, '_cosyvoice-python', 'lib',
            f'python{sys.version_info.major}.{sys.version_info.minor}',
            'site-packages')
        if os.path.isdir(sidecar_site2) and sidecar_site2 not in sys.path:
            sys.path.insert(0, sidecar_site2)
        logger.info("Sidecar Python site-packages added: %s", sidecar_site)


_discover_sidecar_python()

# Heavy import AFTER sidecar discovery so it resolves from _cosyvoice-python/
# (setup-optional) when not present in the base environment.
import numpy as np  # noqa: E402


# ------------------------------------------------------------------
# Monkey-patch: PyTorch >= 2.4 — CosyVoice2 creates the model with
# init_empty_weights() (meta device), then:
#   1. load_state_dict() silently fails on meta params (weights not loaded)
#   2. model.to(device) raises "Cannot copy out of meta tensor; no data!"
#
# Fix: patch load_state_dict to pass assign=True for meta params,
#      and patch to() to fall back to to_empty() for meta → real device.
# ------------------------------------------------------------------
def _install_meta_device_patch():
    import torch
    if getattr(torch.nn.Module, "_cosyvoice_patched", False):
        return

    # patch 1: to() → to_empty() when moving from meta device
    _orig_to = torch.nn.Module.to

    def _patched_to(self, *args, **kwargs):
        try:
            return _orig_to(self, *args, **kwargs)
        except RuntimeError as e:
            if "meta tensor" in str(e) or "to_empty" in str(e):
                device = (args[0] if args
                          else kwargs.get("device", "cuda"))
                return self.to_empty(device)
            raise

    torch.nn.Module.to = _patched_to

    # patch 2: load_state_dict — when model params are on meta device,
    #          normal copy is a no-op; use assign=True to materialize.
    _orig_load_state_dict = torch.nn.Module.load_state_dict

    def _patched_load_state_dict(self, state_dict, strict=True, assign=False):
        # Detect meta params → force assign=True so weights are materialized
        if not assign:
            try:
                p = next(self.parameters())
                if p.device.type == "meta":
                    assign = True
            except StopIteration:
                pass
        return _orig_load_state_dict(self, state_dict, strict=strict,
                                     assign=assign)

    torch.nn.Module.load_state_dict = _patched_load_state_dict
    torch.nn.Module._cosyvoice_patched = True


_install_meta_device_patch()


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
                 model: str | None = None, instruct: str | None = None):
        self.speaker_wav = speaker_wav
        self.model_name = model or _DEFAULT_MODEL
        # Optional natural-language style/emotion instruction (语气/情感),
        # e.g. "用亲切自然的语气" / "热情激昂地讲解". When set, this text is
        # prepended to the whisper transcription as prompt_text, so the LLM
        # uses it for style control during zero-shot cloning.
        self.instruct = (instruct or "").strip()
        self._model_dir: str | None = None  # resolved local path
        self._prompt_text: str | None = None  # whisper transcription, lazy init
        # Cached speaker embedding ID for consistent voice across segments.
        # Once initialized via add_zero_shot_spk(), all subsequent synthesize()
        # calls reuse this ID to avoid per-call embedding variation.
        self._spk_id: str | None = None

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
        if _MODEL is not None:
            return _MODEL
        with _MODEL_LOCK:
            # double-check after acquiring lock
            if _MODEL is not None:
                return _MODEL
            try:
                # CosyVoice2 is the latest model supporting zero-shot cloning
                from cosyvoice.cli.cosyvoice import CosyVoice2

                if self._model_dir is None:
                    self._model_dir = self._resolve_model_path(self.model_name)

                import torch
                fp16 = torch.cuda.is_available()
                logger.info(
                    "加载 CosyVoice2 模型 %s (fp16=%s) …",
                    self._model_dir, fp16)
                _MODEL = CosyVoice2(
                    self._model_dir,
                    load_jit=False,
                    load_trt=False,
                    fp16=fp16,
                )
            except ImportError as e:
                raise RuntimeError(
                    "未安装 CosyVoice：请 `pip install cosyvoice`，"
                    "然后下载模型：\n"
                    "  huggingface-cli download FunAudioLLM/CosyVoice2-0.5B "
                    "--local-dir pretrained_models/CosyVoice2-0.5B\n\n"
                    "注意：CosyVoice2 需要 Python < 3.13 且建议有 NVIDIA GPU。"
                ) from e
            logger.info("CosyVoice2 模型加载完成")
        return _MODEL

    @staticmethod
    def _get_whisper():
        """Lazy-loaded process-global whisper model for transcribing reference audio."""
        global _WHISPER
        if _WHISPER is not None:
            return _WHISPER
        with _WHISPER_LOCK:
            if _WHISPER is not None:
                return _WHISPER
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

        PyTorch 2.x ``scaled_dot_product_attention`` on CUDA has a shape
        assertion bug (``key.size(1) == value.size(1)``) on short audio
        clips when FlashAttention or MemEfficientAttention backends are
        active.  We wrap the call with ``sdp_kernel`` to force the safe
        Math (vanilla bmm) backend.
        """
        try:
            import torch
            from contextlib import nullcontext
            model = CosyVoiceTTS._get_whisper()
            # Disable flash/mem-efficient attention (triggers SDPA shape
            # assertion on short audio with CUDA + PyTorch ≥ 2.0).
            try:
                sdp_ctx = torch.backends.cuda.sdp_kernel(
                    enable_flash=False,
                    enable_mem_efficient=False,
                    enable_math=True,
                )
            except AttributeError:
                sdp_ctx = nullcontext()
            with sdp_ctx:
                result = model.transcribe(
                    wav_path, language="zh", task="transcribe",
                    verbose=None, fp16=False,
                )
            text = result.get("text", "").strip()
            if text:
                logger.info("参考音频转写结果: 「%s」", text)
            else:
                logger.warning("参考音频转写结果为空")
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

    def _ensure_spk_cached(self, model, prompt_wav: str) -> str:
        """Cache speaker embedding once; return zero_shot_spk_id for reuse.

        CosyVoice2's ``add_zero_shot_spk`` extracts and stores speaker
        embeddings (LLM + flow) keyed by ``zero_shot_spk_id``.  Subsequent
        ``inference_zero_shot`` calls with this ID skip re-extraction and
        produce **consistent** voice across all segments — solving the
        "different voice per segment" problem.
        """
        if self._spk_id is not None:
            return self._spk_id
        # Use a stable ID derived from the reference audio path so the
        # cache survives across provider recreations within the same process.
        import hashlib
        self._spk_id = "spk_" + hashlib.md5(
            prompt_wav.encode()).hexdigest()[:12]
        try:
            # Ensure whisper transcription is ready (needed by add_zero_shot_spk)
            if self._prompt_text is None:
                with _PROMPT_LOCK:
                    if self._prompt_text is None:
                        self._prompt_text = self._transcribe_ref_audio(
                            prompt_wav)
                        if self._prompt_text:
                            logger.info("参考音频转写完成（%d 字）",
                                        len(self._prompt_text))
                        else:
                            logger.warning(
                                "参考音频转写失败，使用空文本（音质可能受损）")
                            self._prompt_text = ""
            model.add_zero_shot_spk(
                prompt_text=self._prompt_text,
                prompt_wav=prompt_wav,
                zero_shot_spk_id=self._spk_id,
            )
            logger.info("说话人缓存初始化完成: %s", self._spk_id)
        except Exception as e:
            # If add_zero_shot_spk fails (e.g. API change), clear the ID
            # and fall back to per-call zero-shot (less consistent but works).
            logger.warning("说话人缓存失败，回退到逐句克隆：%s", e)
            self._spk_id = ""
        return self._spk_id

    def synthesize(self, text: str, out_stem: Path) -> Path:
        if not self.speaker_wav or not Path(self.speaker_wav).exists():
            raise RuntimeError(
                "未选择声音样本：请在「设置 → 声音库」录入并选用一个声音")

        path = Path(out_stem).with_suffix(".wav")
        path.parent.mkdir(parents=True, exist_ok=True)

        import soundfile as sf

        model = self._get_model()
        prompt_wav = self._resolve_ref_wav(self.speaker_wav)

        with _INFERENCE_LOCK:
            # Ensure speaker embedding is cached (one-time extraction).
            # This guarantees consistent voice across all segments.
            spk_id = self._ensure_spk_cached(model, prompt_wav)

            # Build prompt_text: whisper transcription of reference audio,
            # optionally prepended with the instruct text for style control.
            # CosyVoice2's LLM uses prompt_text as context — prepending the
            # style instruction (e.g. "热情激昂地讲解") lets the model
            # follow the desired prosody WITHOUT reading the instruction
            # aloud (it's treated as metadata, not speech content).
            prompt_text = self._prompt_text or ""
            if self.instruct and self.instruct not in prompt_text:
                prompt_text = self.instruct + "，" + prompt_text

            results = model.inference_zero_shot(
                tts_text=text,
                prompt_text=prompt_text,
                prompt_wav=prompt_wav,
                zero_shot_spk_id=spk_id,
                stream=False,
            )
            for result in results:
                audio = result["tts_speech"]  # shape: [1, samples]
                sf.write(str(path), audio.squeeze(0).numpy(),
                         model.sample_rate)

        if not path.exists():
            raise RuntimeError(
                "CosyVoice2 推理未产生输出文件")
        return path
