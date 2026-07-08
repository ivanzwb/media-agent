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
INTERNAL_DIR="${BUNDLE_DIR}/_internal"

# ── 1. Playwright + Chromium ─────────────────────
echo "[1/4] Playwright + Chromium ..."

if [ -d "$INTERNAL_DIR/playwright" ]; then
    echo "  [√] Playwright 库已打包"
else
    echo "  [×] Playwright 未打包，请解压完整压缩包（含 _internal 目录）"
    echo ""
    skip_to_end=true
fi

if [ "$skip_to_end" != true ]; then
    # Check if Chromium browser is already installed
    PW_CACHE="$HOME/Library/Caches/ms-playwright"
    CHROMIUM_INSTALLED=false
    if [ -d "$PW_CACHE" ]; then
        for d in "$PW_CACHE"/chromium-*/chrome; do
            [ -x "$d" ] && CHROMIUM_INSTALLED=true && break
        done
    fi

    if [ "$CHROMIUM_INSTALLED" = true ]; then
        echo "  [√] Chromium 已安装"
    elif [ -x "$MEDIA_AGENT" ]; then
        echo "  正在下载 Chromium 浏览器（约 300MB，首次只需一次）..."
        if "$MEDIA_AGENT" --run-module playwright install chromium 2>/dev/null; then
            echo "  [√] Chromium 安装完成"
        else
            echo "  [×] Chromium 自动安装失败"
            echo ""
            echo "  手动安装方式："
            echo "    1. 确保已安装 Python 3.11+"
            echo "    2. pip install playwright"
            echo "    3. python -m playwright install chromium"
        fi
    else
        echo "  [×] 找不到 $MEDIA_AGENT"
    fi
fi
echo ""

# ── 2. fish-audio-sdk ──────────────────────────
echo "[2/4] fish-audio-sdk ..."
echo "  [√] fish-audio-sdk 已打包进主程序，无需额外安装"
echo ""

# ── 3. ffmpeg ──────────────────────────────────
echo "[3/4] ffmpeg ..."
if command -v ffmpeg >/dev/null 2>&1; then
    echo "  [√] ffmpeg 已安装: $(which ffmpeg)"
else
    echo "  [×] ffmpeg 未找到"
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
echo "  3. 启动 Media Agent，在“设置”页把 TTS Provider 切换为 cosyvoice"
echo ""
echo "============================================"
echo "  安装完成！"
echo "  如有问题请提交 Issue: https://github.com/ivanzwb/media-agent/issues"
echo "============================================"
