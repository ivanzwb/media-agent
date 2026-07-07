#!/bin/bash
set -e

echo "============================================"
echo " Media Agent - 可选依赖安装"
echo "============================================"
echo ""
echo "本脚本自动检测并安装缺失的可选组件。"
echo ""
echo "  [1] Playwright + Chromium（用于 JS 渲染抓取）"
echo "  [2] fish-audio-sdk（云端声音克隆 TTS）"
echo "  [3] ffmpeg（视频合成 — 必须自己装）"
echo "  [4] CosyVoice（本地声音复刻 — 需 GPU）"
echo ""
echo "============================================"
echo ""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUNDLE_DIR="$SCRIPT_DIR"
# If called from inside media-agent dir, use that; otherwise find it
if [ ! -f "$BUNDLE_DIR/media-agent" ]; then
    BUNDLE_DIR="$SCRIPT_DIR/media-agent"
fi
MEDIA_AGENT="${BUNDLE_DIR}/media-agent"

# ── 1. playwright + chromium ───────────────────
echo "[1/4] Playwright + Chromium ..."
if [ -x "$MEDIA_AGENT" ]; then
    if "$MEDIA_AGENT" -c "import playwright" 2>/dev/null; then
        echo "  [✓] Playwright 库已打包"
    else
        echo "  [✗] Playwright 库缺失，需重建打包"
    fi
    echo "  正在下载 Chromium 浏览器（约 300MB，首次只需一次）..."
    if "$MEDIA_AGENT" -m playwright install chromium 2>/dev/null; then
        echo "  [✓] Chromium 安装完成"
    else
        echo "  [✗] Chromium 安装失败"
    fi
else
    echo "  [✗] 找不到 $MEDIA_AGENT"
fi
echo ""

# ── 2. fish-audio-sdk ──────────────────────────
echo "[2/4] fish-audio-sdk ..."
echo "  [✓] fish-audio-sdk 已打包进主程序，无需额外安装"
echo ""

# ── 3. ffmpeg ──────────────────────────────────
echo "[3/4] ffmpeg ..."
if command -v ffmpeg >/dev/null 2>&1; then
    echo "  [✓] ffmpeg 已安装: $(which ffmpeg)"
else
    echo "  [✗] ffmpeg 未找到"
    echo ""
    echo "  macOS 推荐安装方式:"
    echo "    brew install ffmpeg"
    echo ""
    echo "  或手动下载: https://ffmpeg.org/download.html"
fi
echo ""

# ── 4. CosyVoice ───────────────────────────────
echo "[4/4] CosyVoice（本地声音复刻，可选）..."
echo ""
echo "  CosyVoice 需要 Python < 3.13 + NVIDIA GPU + 显存 4GB+"
echo "  无法打包进单文件，需另外安装："
echo ""
echo "  1. 创建独立 Python 环境（Python 3.11）:"
echo "     conda create -n cosyvoice python=3.11"
echo "     conda activate cosyvoice"
echo ""
echo "  2. 安装依赖:"
echo "     pip install \"setuptools<70\" cosyvoice"
echo ""
echo "  3. 启动 CosyVoice HTTP 服务:"
echo "     python -m app.tts.providers.cosyvoice_http --port 8888"
echo ""
echo "  4. 在 Media Agent 设置页配置:"
echo "     TTS Provider: cosyvoice"
echo "     API Base: http://127.0.0.1:8888"
echo ""
echo "============================================"
echo "  安装完成！"
echo "  如有问题请提交 Issue: https://github.com/ivanzwb/media-agent/issues"
echo "============================================"
