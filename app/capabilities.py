"""Runtime capability probes for optional, environment-heavy features.

Some features (CosyVoice voice cloning, SadTalker lip-sync) depend on native
libraries that may be missing or unloadable on a given machine (missing
Microsoft VC++ runtime, torch/onnxruntime version mismatch, no GPU, etc.).
The Settings UI queries these probes to disable/gray-out options the machine
cannot actually use, instead of letting the user pick them and then hit a
cryptic runtime error (e.g. WinError 1114 c10.dll init failure).
"""
from __future__ import annotations

import functools
import json
import logging
import subprocess
import sys

logger = logging.getLogger(__name__)

# Probe CosyVoice's native import chain in a SUBPROCESS so a broken native DLL
# (onnxruntime / torch c10.dll → WinError 1114) can't destabilize or slow the
# main server process. The first failing import decides the reason.
_COSY_PROBE = r"""
import json, sys
CHECKS = [
    ("numpy", "缺少 numpy 依赖"),
    ("torch", "torch 无法加载（常见：缺少 Microsoft Visual C++ 运行库，或安装损坏）"),
    ("torchaudio", "缺少 torchaudio（需与 torch 版本匹配）"),
    ("onnxruntime", "onnxruntime 无法加载（常见：缺少 Microsoft Visual C++ 运行库）"),
    ("cosyvoice.cli.cosyvoice", "未安装 CosyVoice 或其依赖不完整"),
]
for mod, reason in CHECKS:
    try:
        __import__(mod)
    except BaseException as e:  # ImportError / OSError(WinError 1114) / etc.
        print(json.dumps({"ok": False, "reason": reason,
                          "detail": (type(e).__name__ + ": " + str(e))[:200]},
                         ensure_ascii=False))
        sys.exit(0)
print(json.dumps({"ok": True, "reason": "可用"}, ensure_ascii=False))
"""


@functools.lru_cache(maxsize=1)
def cosyvoice_capability() -> dict:
    """Best-effort: can CosyVoice TTS run in this environment?

    Returns ``{"available": bool, "reason": str, "detail": str}``. Cached for
    the process lifetime (deps don't change without a restart); call
    :func:`refresh` to re-probe.
    """
    try:
        if getattr(sys, "frozen", False):
            return _cosyvoice_capability_frozen()
        proc = subprocess.run(
            [sys.executable, "-c", _COSY_PROBE],
            capture_output=True, text=True, timeout=180)
        lines = (proc.stdout or "").strip().splitlines()
        if lines:
            data = json.loads(lines[-1])
            return {"available": bool(data.get("ok")),
                    "reason": data.get("reason") or "",
                    "detail": data.get("detail", "")}
        return {"available": False, "reason": "探测 CosyVoice 依赖失败",
                "detail": (proc.stderr or "")[:200]}
    except Exception as e:  # noqa: BLE001 - never let a probe crash the caller
        logger.warning("cosyvoice capability probe failed: %s", e)
        return {"available": False, "reason": f"探测失败：{e}", "detail": ""}


def _cosyvoice_capability_frozen() -> dict:
    checks = [
        ("torch", "torch 无法加载（常见：缺少 Microsoft Visual C++ 运行库）"),
        ("onnxruntime", "onnxruntime 无法加载（常见：缺少 Microsoft Visual C++ 运行库）"),
        ("cosyvoice.cli.cosyvoice", "未安装 CosyVoice 或其依赖不完整"),
    ]
    for mod, reason in checks:
        try:
            proc = subprocess.run([sys.executable, "--check-module", mod],
                                  capture_output=True, timeout=180)
        except Exception as e:  # noqa: BLE001
            return {"available": False, "reason": reason, "detail": str(e)[:200]}
        if proc.returncode != 0:
            return {"available": False, "reason": reason, "detail": ""}
    return {"available": True, "reason": "可用"}


def sadtalker_capability(config) -> dict:
    """Can the digital-human presenter use SadTalker lip-sync on this machine?

    Cheap filesystem check (no heavy import); depends on the live config so it
    is intentionally NOT cached.
    """
    from app.video.avatar import AvatarSpec, sadtalker_available
    d = (getattr(config, "sadtalker_dir", None) or "").strip()
    if not d:
        return {"available": False,
                "reason": "未配置 SadTalker（配置含 inference.py 的本地 SadTalker 目录后可用）"}
    spec = AvatarSpec(sadtalker_dir=d,
                      sadtalker_python=getattr(config, "sadtalker_python", None))
    if sadtalker_available(spec):
        return {"available": True, "reason": "可用"}
    return {"available": False,
            "reason": f"SadTalker 目录无效（未找到 inference.py）：{d}"}


def refresh() -> None:
    """Clear cached probes so the next query re-detects (e.g. after install)."""
    cosyvoice_capability.cache_clear()
