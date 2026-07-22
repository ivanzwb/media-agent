#!/bin/bash
set -e

echo "============================================"
echo " Media Agent - Optional Component Setup"
echo "============================================"
echo ""
echo "This script auto-detects and installs missing optional components."
echo ""
echo "  [1] Playwright + Chromium (for JS-rendered page scraping)"
echo "  [2] ffmpeg (video compositing — install manually)"
echo "  [3] CosyVoice (managed runtime; install in Settings)"
echo "  [4] SadTalker (managed lip-sync runtime; optional)"
echo ""
echo "============================================"
echo ""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUNDLE_DIR="$SCRIPT_DIR"
if [ ! -f "$BUNDLE_DIR/media-agent" ]; then
    BUNDLE_DIR="$SCRIPT_DIR/media-agent"
fi
MEDIA_AGENT="${BUNDLE_DIR}/media-agent"
INTERNAL_DIR="${BUNDLE_DIR}/_internal"
COSYVOICE_DIR="${BUNDLE_DIR}/_cosyvoice-python"

# ── 1. Playwright + Chromium ─────────────────────
echo "[1/4] Playwright + Chromium ..."

if [ -d "$INTERNAL_DIR/playwright" ]; then
    echo "  [OK] Playwright library is bundled"
else
    echo "  [SKIP] Playwright not found — re-extract full archive"
    echo ""
    skip_to_end=true
fi

if [ "$skip_to_end" != true ]; then
    PW_CACHE="$HOME/Library/Caches/ms-playwright"
    CHROMIUM_INSTALLED=false
    if [ -d "$PW_CACHE" ]; then
        # The app scrapes headless, which uses the ~115 MB headless shell —
        # accept either the headless shell OR a full Chromium if present.
        # Detect by the versioned directory (executable path varies by OS).
        for d in "$PW_CACHE"/chromium_headless_shell-* "$PW_CACHE"/chromium-*; do
            [ -d "$d" ] && CHROMIUM_INSTALLED=true && break
        done
    fi

    if [ "$CHROMIUM_INSTALLED" = true ]; then
        echo "  [OK] Chromium already installed"
    elif [ -x "$MEDIA_AGENT" ]; then
        echo "  Downloading Chromium headless shell (~115 MB, first time only)..."
        # Raise Playwright's download timeout (default 30s) so a slow
        # connection doesn't abort and re-download from scratch.
        export PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=180000
        if "$MEDIA_AGENT" --run-module playwright install chromium-headless-shell 2>/dev/null; then
            echo "  [OK] Chromium installed"
        else
            echo "  [FAIL] Chromium auto-install failed"
            echo ""
            echo "  Manual install:"
            echo "    1. Make sure Python 3.11+ is installed"
            echo "    2. pip install playwright"
            echo "    3. python -m playwright install chromium-headless-shell"
        fi
    else
        echo "  [SKIP] $MEDIA_AGENT not found"
    fi
fi
echo ""

# ── 2. ffmpeg ──────────────────────────────────
echo "[2/4] ffmpeg ..."
if command -v ffmpeg >/dev/null 2>&1; then
    echo "  [OK] ffmpeg found: $(which ffmpeg)"
else
    echo "  [SKIP] ffmpeg not found"
    echo ""
    echo "  macOS: brew install ffmpeg"
    echo "  Or download: https://ffmpeg.org/download.html"
fi
echo ""

# ── 3. CosyVoice (managed runtime) ────────────
echo "[3/4] CosyVoice (local voice cloning)..."
echo "  Managed runtimes are available on Windows and macOS (Intel/Apple Silicon)."
echo "  Install it from Settings -> Voice and Video; no Python/source paths are used."
echo "  Use the Settings installer; macOS uses CPU and may be slow."
echo ""

# ── 4. SadTalker (digital-human presenter lip-sync, optional) ──
echo "[4/4] SadTalker (数字人主播口型同步, 可选)..."
echo "  Use the Settings installer; macOS uses CPU and may be slow."
echo "  If omitted, video generation automatically uses a static avatar."
echo ""

echo "============================================"
echo "  Setup complete!"
echo "  Report issues: https://github.com/ivanzwb/media-agent/issues"
echo "============================================"
