#!/usr/bin/env bash
set -euo pipefail

kind="${1:?usage: prepare-managed-runtime-dev.sh sadtalker|cosyvoice [data-dir]}"
case "$kind" in sadtalker|cosyvoice) ;; *) echo "unsupported runtime: $kind" >&2; exit 2;; esac

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
data_dir="${2:-$root/data}"
machine="$(uname -m)"
case "$machine" in
  arm64|aarch64) arch=arm64; miniconda_arch=arm64 ;;
  x86_64|amd64) arch=x64; miniconda_arch=x86_64 ;;
  *) echo "macOS architecture is unsupported: $machine" >&2; exit 2 ;;
esac
[[ "$(uname -s)" == Darwin ]] || { echo "This script supports macOS only." >&2; exit 2; }

build="$root/.runtime-build"
conda_root="$build/miniconda-macos-$arch"
env_dir="$build/$kind-macos-$arch-env"
artifacts="$build/artifacts"
asset="$kind-runtime-macos-$arch-cpu.tar.gz"
mkdir -p "$build" "$artifacts"

conda="$conda_root/bin/conda"
if [[ ! -x "$conda" ]]; then
  installer="$build/Miniconda3-py310_24.7.1-0-MacOSX-$miniconda_arch.sh"
  url="https://repo.anaconda.com/miniconda/$(basename "$installer")"
  echo "Downloading pinned Miniconda for $machine..."
  curl -fL --retry 8 --retry-all-errors --connect-timeout 30 \
    --speed-limit 1024 --speed-time 60 -C - -o "$installer" "$url"
  bash "$installer" -b -p "$conda_root"
fi

if [[ ! -x "$env_dir/bin/python" ]]; then
  "$conda" create -y -p "$env_dir" python=3.10 pip
fi
python="$env_dir/bin/python"
"$python" -m pip install --upgrade "pip<25" "setuptools<81" wheel

if [[ "$kind" == sadtalker ]]; then
  "$python" -m pip install torch==2.0.1 torchvision==0.15.2 torchaudio==2.0.2
  "$python" -m pip install Cython==0.29.36
  "$python" -m pip install --no-deps \
    basicsr==1.4.2 facexlib==0.3.0 gfpgan==1.3.8
  "$python" -m pip install -r "$root/packaging/sadtalker-runtime-requirements.txt"
  source_dir="$env_dir/sadtalker-src"
  commit=cd4c0465ae0b54a6f85af57f5c65fec9fe23e7f8
  if [[ ! -f "$source_dir/.media-agent-source-commit" ]] ||
     [[ "$(<"$source_dir/.media-agent-source-commit")" != "$commit" ]]; then
    rm -rf "$source_dir"
    git clone https://github.com/OpenTalker/SadTalker.git "$source_dir"
    git -C "$source_dir" checkout "$commit"
    rm -rf "$source_dir/.git"
    printf '%s\n' "$commit" > "$source_dir/.media-agent-source-commit"
  fi
  site="$("$python" -c 'import site; print(site.getsitepackages()[0])')"
  printf '%s\n' "$source_dir" > "$site/sadtalker-source.pth"
  "$python" -c "import cv2, torch, torchvision, face_alignment, safetensors; assert not torch.cuda.is_available()"
  "$python" "$source_dir/inference.py" --help >/dev/null
  "$python" "$root/packaging/pack_sadtalker_runtime.py" \
    --prefix "$env_dir" --output-dir "$artifacts" \
    --asset-name "$asset" --keep-archive
  prepare="$root/packaging/prepare_sadtalker_dev.py"
else
  torch_version=2.3.1
  [[ "$arch" == x64 ]] && torch_version=2.2.2
  "$python" -m pip install "torch==$torch_version" "torchaudio==$torch_version"
  "$python" -m pip install -r "$root/packaging/cosyvoice-runtime-requirements.txt"
  source_dir="$env_dir/cosyvoice-src"
  commit=074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc
  if [[ ! -f "$source_dir/.media-agent-source-commit" ]] ||
     [[ "$(<"$source_dir/.media-agent-source-commit")" != "$commit" ]]; then
    rm -rf "$source_dir"
    git clone --recursive https://github.com/FunAudioLLM/CosyVoice.git "$source_dir"
    git -C "$source_dir" checkout "$commit"
    git -C "$source_dir" submodule update --init --recursive
    rm -rf "$source_dir/.git"
    printf '%s\n' "$commit" > "$source_dir/.media-agent-source-commit"
  fi
  site="$("$python" -c 'import site; print(site.getsitepackages()[0])')"
  printf '%s\n' "$source_dir" > "$site/cosyvoice-source.pth"
  "$python" -c "import torch, onnxruntime, whisper; from cosyvoice.cli.cosyvoice import CosyVoice2; assert not torch.cuda.is_available()"
  "$python" "$root/packaging/pack_cosyvoice_runtime.py" \
    --prefix "$env_dir" --output-dir "$artifacts" \
    --asset-name "$asset" --keep-archive
  prepare="$root/packaging/prepare_cosyvoice_dev.py"
fi

project_python="$root/.venv/bin/python"
[[ -x "$project_python" ]] || project_python=python3
"$project_python" "$prepare" --archive "$artifacts/$asset" --data-dir "$data_dir"
echo "$kind macOS $arch CPU runtime and models are ready."
