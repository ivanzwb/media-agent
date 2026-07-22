#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${1:-$ROOT/data}"
BUILD="$ROOT/.runtime-build"
CONDA_ROOT="$BUILD/miniconda-macos"
ENV_DIR="$BUILD/cosyvoice-macos-env"
ARTIFACTS="$BUILD/cosyvoice-artifacts"
COMMIT="074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc"
MACHINE="$(uname -m)"
ARCH="$([[ "$MACHINE" == "arm64" ]] && echo arm64 || echo x64)"
CONDA_ARCH="$([[ "$MACHINE" == "arm64" ]] && echo arm64 || echo x86_64)"
ASSET="cosyvoice-runtime-macos-${ARCH}-cpu.tar.gz"

mkdir -p "$BUILD" "$ARTIFACTS"
if [[ ! -x "$CONDA_ROOT/bin/conda" ]]; then
  INSTALLER="$BUILD/miniconda-macos.sh"
  curl -fL --retry 8 --continue-at - \
    -o "$INSTALLER" \
    "https://repo.anaconda.com/miniconda/Miniconda3-py310_24.7.1-0-MacOSX-${CONDA_ARCH}.sh"
  bash "$INSTALLER" -b -p "$CONDA_ROOT"
fi
CONDA="$CONDA_ROOT/bin/conda"
[[ -x "$ENV_DIR/bin/python" ]] || "$CONDA" create -y -p "$ENV_DIR" python=3.10 pip
PY="$ENV_DIR/bin/python"

"$PY" -m pip install --upgrade "pip<25" "setuptools<81" wheel
"$PY" -m pip install torch==2.3.1 torchaudio==2.3.1
"$PY" -m pip install -r "$ROOT/packaging/cosyvoice-runtime-requirements.txt"

SRC="$ENV_DIR/cosyvoice-src"
if [[ ! -f "$SRC/.media-agent-source-commit" ]] ||
   [[ "$(cat "$SRC/.media-agent-source-commit")" != "$COMMIT" ]]; then
  rm -rf "$SRC"
  git clone --recursive https://github.com/FunAudioLLM/CosyVoice.git "$SRC"
  git -C "$SRC" checkout "$COMMIT"
  git -C "$SRC" submodule update --init --recursive
  rm -rf "$SRC/.git"
  printf '%s\n' "$COMMIT" > "$SRC/.media-agent-source-commit"
fi
SP="$("$PY" -c 'import site; print(site.getsitepackages()[0])')"
printf '%s\n' "$SRC" > "$SP/cosyvoice-source.pth"
"$PY" -c "import torch, onnxruntime, whisper; from cosyvoice.cli.cosyvoice import CosyVoice2"
"$PY" "$ROOT/packaging/pack_cosyvoice_runtime.py" \
  --prefix "$ENV_DIR" --output-dir "$ARTIFACTS" \
  --asset-name "$ASSET" --keep-archive
PROJECT_PY="$ROOT/.venv/bin/python"
[[ -x "$PROJECT_PY" ]] || PROJECT_PY=python3
"$PROJECT_PY" "$ROOT/packaging/prepare_cosyvoice_dev.py" \
  --archive "$ARTIFACTS/$ASSET" --data-dir "$DATA_DIR"
