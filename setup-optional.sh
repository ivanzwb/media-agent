#!/bin/bash
set -e

echo "============================================"
echo " Media Agent - Optional Component Setup"
echo "============================================"
echo ""
echo "This script auto-detects and installs missing optional components."
echo ""
echo "  [1] Playwright + Chromium (for JS-rendered page scraping)"
echo "  [2] fish-audio-sdk (cloud voice-cloning TTS)"
echo "  [3] ffmpeg (video compositing — install manually)"
echo "  [4] CosyVoice (local voice cloning via embedded Python)"
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

# ── 2. fish-audio-sdk ──────────────────────────
echo "[2/4] fish-audio-sdk ..."
echo "  [OK] fish-audio-sdk is bundled in the package"
echo ""

# ── 3. ffmpeg ──────────────────────────────────
echo "[3/4] ffmpeg ..."
if command -v ffmpeg >/dev/null 2>&1; then
    echo "  [OK] ffmpeg found: $(which ffmpeg)"
else
    echo "  [SKIP] ffmpeg not found"
    echo ""
    echo "  macOS: brew install ffmpeg"
    echo "  Or download: https://ffmpeg.org/download.html"
fi
echo ""

# ── 4. CosyVoice (embedded Python sidecar) ────
echo "[4/4] CosyVoice (local voice cloning)..."

# Skip if already set up
if [ -f "$COSYVOICE_DIR/bin/python3" ]; then
    echo "  [OK] Embedded Python already set up"
    PYTHON="$COSYVOICE_DIR/bin/python3"
else
    # Find the embeddable Python archive in _internal/packaging/
    EMBED_ARC="$INTERNAL_DIR/packaging/python-embed-macos.tar.gz"
    GET_PIP="$INTERNAL_DIR/packaging/get-pip.py"

    if [ ! -f "$EMBED_ARC" ]; then
        echo "  [FAIL] Embeddable Python archive not found at:"
        echo "         $EMBED_ARC"
        echo ""
        return 2>/dev/null || exit 1
    fi

    # Extract embeddable Python
    echo "  Extracting embedded Python..."
    mkdir -p "$COSYVOICE_DIR"
    tar xzf "$EMBED_ARC" -C "$COSYVOICE_DIR" --strip-components=1 2>/dev/null || \
        tar xzf "$EMBED_ARC" -C "$COSYVOICE_DIR"

    # Find python binary
    PYTHON=""
    for candidate in "$COSYVOICE_DIR/bin/python3" "$COSYVOICE_DIR/python3" "$COSYVOICE_DIR/bin/python"; do
        if [ -x "$candidate" ]; then
            PYTHON="$candidate"
            break
        fi
    done

    if [ -z "$PYTHON" ]; then
        echo "  [FAIL] Failed to extract embeddable Python"
        return 2>/dev/null || exit 1
    fi
    echo "  [OK] Python extracted: $PYTHON"

    # Install pip
    echo "  Installing pip..."
    "$PYTHON" "$GET_PIP" --quiet
    echo "  [OK] pip installed"

    # Install PyTorch (CPU) + CosyVoice
    echo "  Installing PyTorch (CPU) + CosyVoice (may take a few minutes)..."
    "$PYTHON" -m pip install torch torchvision torchaudio \
        --index-url https://download.pytorch.org/whl/cpu --quiet
    echo "  [OK] PyTorch installed"

    "$PYTHON" -m pip install cosyvoice --quiet
    echo "  [OK] CosyVoice installed"
fi

# Download model
echo "  Checking CosyVoice model..."
MODEL_DIR="$BUNDLE_DIR/pretrained_models/CosyVoice2-0.5B"
if [ -f "$MODEL_DIR/model.pt" ]; then
    echo "  [OK] Model already downloaded"
else
    echo "  Downloading CosyVoice2-0.5B model (~1.5 GB, first time only)..."
    mkdir -p "$MODEL_DIR"
    "$PYTHON" -m pip install "huggingface_hub[cli]" --quiet
    "$PYTHON" -m huggingface_hub.cli download \
        FunAudioLLM/CosyVoice2-0.5B --local-dir "$MODEL_DIR"
    echo "  [OK] Model downloaded"
fi

echo ""
echo "  [OK] CosyVoice is ready to use!"
echo "  Start Media Agent, then set TTS Provider to 'cosyvoice' in Settings."
echo ""
echo "============================================"
echo "  Setup complete!"
echo "  Report issues: https://github.com/ivanzwb/media-agent/issues"
echo "============================================"
