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
echo "  [3] CosyVoice (local voice cloning via embedded Python)"
echo "  [4] SadTalker (digital-human presenter lip-sync — optional, GPU)"
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

# ── 3. CosyVoice (embedded Python sidecar) ────
echo "[3/4] CosyVoice (local voice cloning)..."

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
    # Note: `huggingface_hub[cli]` extra no longer exists in >=1.23.0,
    # and `-m huggingface_hub.cli download` fails because cli is a package.
    "$PYTHON" -m pip install huggingface_hub --quiet
    # Disable the Xet backend — its CAS server (cas-server.xethub.hf.co) can
    # return 401 and isn't served by mirrors; classic HTTP download is reliable.
    export HF_HUB_DISABLE_XET=1
    export HF_XET_DISABLE=1
    export HF_HUB_DOWNLOAD_TIMEOUT=60
    # Retry loop — snapshot_download resumes partial files, so re-running
    # continues from where a timeout/drop left off.
    _dl_n=0
    until "$PYTHON" -c "from huggingface_hub import snapshot_download; snapshot_download('FunAudioLLM/CosyVoice2-0.5B', local_dir='$MODEL_DIR', max_workers=2)"; do
        _dl_n=$((_dl_n + 1))
        if [ "$_dl_n" -ge 8 ]; then
            echo "  [FAIL] Model download failed — re-run this script to resume."
            break
        fi
        echo "  下载中断，第 $_dl_n/8 次重试（断点续传）..."
        sleep 3
    done
    [ "$_dl_n" -lt 8 ] && echo "  [OK] Model downloaded"
fi

echo ""
echo "  [OK] CosyVoice is ready to use!"
echo "  Start Media Agent, then set TTS Provider to 'cosyvoice' in Settings."
echo ""

# ── 4. SadTalker (digital-human presenter lip-sync, optional) ──
echo "[4/4] SadTalker (数字人主播口型同步, 可选)..."
SADTALKER_DIR="$BUNDLE_DIR/SadTalker"
if [ -f "$SADTALKER_DIR/inference.py" ] && [ -d "$SADTALKER_DIR/checkpoints" ]; then
    echo "  [OK] SadTalker 已就绪：$SADTALKER_DIR"
    echo "  在「设置 → 视频 → 数字人主播」把 SadTalker 目录设为上面的路径。"
else
    echo "  SadTalker 为数字人提供口型同步，体积大（模型 ~5GB）且需要 NVIDIA GPU；"
    echo "  不安装时数字人会用静态头像叠加（仍可用）。"
    printf "  现在安装 SadTalker？[y/N] "
    read -r _ans || _ans=""
    if [ "$_ans" = "y" ] || [ "$_ans" = "Y" ] || [ "$_ans" = "yes" ]; then
        if ! command -v git >/dev/null 2>&1; then
            echo "  [SKIP] 未找到 git，请先安装 git 再重跑本脚本。"
        else
            SPY="${PYTHON:-}"
            if [ ! -x "$SPY" ]; then SPY="$(command -v python3 || command -v python || true)"; fi
            if [ -z "$SPY" ]; then
                echo "  [SKIP] 未找到可用的 Python。"
            else
                echo "  克隆 SadTalker…"
                [ -d "$SADTALKER_DIR/.git" ] || git clone --depth 1 \
                    https://github.com/OpenTalker/SadTalker.git "$SADTALKER_DIR" \
                    || echo "  [WARN] 克隆失败，请检查网络/git。"
                if [ -f "$SADTALKER_DIR/requirements.txt" ]; then
                    echo "  安装 SadTalker 依赖（建议使用匹配 GPU 的 torch）…"
                    "$SPY" -m pip install -r "$SADTALKER_DIR/requirements.txt" \
                        || echo "  [WARN] 依赖未完全安装，请按 SadTalker README 手动补齐（尤其匹配 CUDA 的 torch）。"
                fi
                echo "  下载模型（~5GB，首次）…"
                if [ -f "$SADTALKER_DIR/scripts/download_models.sh" ]; then
                    ( cd "$SADTALKER_DIR" && bash scripts/download_models.sh ) \
                        || echo "  [WARN] 模型下载未完成，可重跑本脚本或手动执行 scripts/download_models.sh。"
                fi
                if [ -d "$SADTALKER_DIR/checkpoints" ]; then
                    echo "  [OK] SadTalker 就绪：$SADTALKER_DIR"
                    echo "  在「设置 → 视频 → 数字人主播」设置该目录并开启数字人。"
                else
                    echo "  [WARN] 缺少 checkpoints，参见 https://github.com/OpenTalker/SadTalker"
                fi
            fi
        fi
    else
        echo "  [SKIP] 跳过 SadTalker（数字人将用静态头像）。"
    fi
fi
echo ""

echo "============================================"
echo "  Setup complete!"
echo "  Report issues: https://github.com/ivanzwb/media-agent/issues"
echo "============================================"
