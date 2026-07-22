#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${1:-$ROOT/data}"
BUILD="$ROOT/.runtime-build"
CONDA_ROOT="$BUILD/miniconda-macos"
ENV_DIR="$BUILD/sadtalker-macos-env"
ARTIFACTS="$BUILD/artifacts"
COMMIT="cd4c0465ae0b54a6f85af57f5c65fec9fe23e7f8"
MACHINE="$(uname -m)"
ARCH="$([[ "$MACHINE" == "arm64" ]] && echo arm64 || echo x64)"
CONDA_ARCH="$([[ "$MACHINE" == "arm64" ]] && echo arm64 || echo x86_64)"
ASSET="sadtalker-runtime-macos-${ARCH}-cpu.tar.gz"

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
"$PY" -m pip install torch==2.0.1 torchvision==0.15.2 torchaudio==2.0.2
"$PY" -m pip install Cython==0.29.36
"$PY" -m pip install --no-deps basicsr==1.4.2 facexlib==0.3.0 gfpgan==1.3.8
"$PY" -m pip install -r "$ROOT/packaging/sadtalker-runtime-requirements.txt"

SRC="$ENV_DIR/sadtalker-src"
if [[ ! -f "$SRC/.media-agent-source-commit" ]] ||
   [[ "$(cat "$SRC/.media-agent-source-commit")" != "$COMMIT" ]]; then
  rm -rf "$SRC"
  git clone https://github.com/OpenTalker/SadTalker.git "$SRC"
  git -C "$SRC" checkout "$COMMIT"
  rm -rf "$SRC/.git"
  printf '%s\n' "$COMMIT" > "$SRC/.media-agent-source-commit"
fi
SP="$("$PY" -c 'import site; print(site.getsitepackages()[0])')"
printf '%s\n' "$SRC" > "$SP/sadtalker-source.pth"
"$PY" "$SRC/inference.py" --help >/dev/null
"$PY" "$ROOT/packaging/pack_sadtalker_runtime.py" \
  --prefix "$ENV_DIR" --output-dir "$ARTIFACTS" \
  --asset-name "$ASSET" --keep-archive
PROJECT_PY="$ROOT/.venv/bin/python"
[[ -x "$PROJECT_PY" ]] || PROJECT_PY=python3
"$PROJECT_PY" "$ROOT/packaging/prepare_sadtalker_dev.py" \
  --archive "$ARTIFACTS/$ASSET" --data-dir "$DATA_DIR"
