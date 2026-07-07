#!/bin/bash
set -e
echo "=== Media Agent macOS Build ==="

# Check Python
python3 -c "import sys; assert sys.version_info >= (3,11), 'Need Python 3.11+'; print(f'Python {sys.version_info.major}.{sys.version_info.minor} OK')"

# Install deps
pip3 install -r requirements.txt
pip3 install "click<8.2" pyinstaller fish-audio-sdk playwright

# Build
pyinstaller --onedir \
    --name "media-agent" \
    --add-data "app/web/templates:app/web/templates" \
    --add-data "app/web/static:app/web/static" \
    --add-data "app/licensing/public_key.b64:app/licensing" \
    --exclude-module "torch" \
    --exclude-module "torchvision" \
    --exclude-module "torchaudio" \
    --exclude-module "transformers" \
    --exclude-module "scipy" \
    --exclude-module "matplotlib" \
    --exclude-module "pandas" \
    --exclude-module "sklearn" \
    --exclude-module "scikit-learn" \
    --exclude-module "librosa" \
    --exclude-module "numba" \
    --exclude-module "soundfile" \
    --exclude-module "onnxruntime" \
    --exclude-module "tensorflow" \
    --exclude-module "pyarrow" \
    --exclude-module "IPython" \
    --exclude-module "jedi" \
    --exclude-module "parso" \
    --exclude-module "pytest" \
    --exclude-module "nbformat" \
    --exclude-module "jsonschema" \
    --exclude-module "lark" \
    --exclude-module "modelscope" \
    --exclude-module "lightning" \
    --exclude-module "hydra" \
    --exclude-module "altair" \
    --hidden-import "uvicorn" \
    --hidden-import "uvicorn.logging" \
    --hidden-import "uvicorn.loops.auto" \
    --hidden-import "uvicorn.protocols.http.auto" \
    --hidden-import "uvicorn.protocols.websockets.auto" \
    --hidden-import "fastapi" \
    --hidden-import "jinja2" \
    --hidden-import "cryptography" \
    --hidden-import "cryptography.fernet" \
    --hidden-import "apscheduler" \
    --hidden-import "apscheduler.triggers.cron" \
    --hidden-import "httpx" \
    --console \
    app/cli.py

rm -rf build media-agent.spec
cp setup-optional.sh dist/media-agent/ 2>/dev/null || true

echo "=== Build complete ==="
ls -lh dist/media-agent/media-agent
echo ""
echo "Run:  dist/media-agent/media-agent serve"
