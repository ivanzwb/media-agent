"""Install a locally packed CosyVoice runtime and pre-download its model."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.cosyvoice_install import (  # noqa: E402
    download_models, install_runtime_archive, runtime_files_ready,
    runtime_smoke, setup_status,
)


def progress(message: str, fraction: float) -> None:
    suffix = "" if fraction < 0 else f" [{fraction * 100:5.1f}%]"
    print(f"{message}{suffix}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--data-dir", default=Path("data"), type=Path)
    parser.add_argument("--proxy", default="")
    args = parser.parse_args()
    data_dir = args.data_dir.resolve()
    if not runtime_files_ready(data_dir):
        install_runtime_archive(
            data_dir, args.archive, progress_cb=progress,
            cleanup_archive=False)
    if not download_models(
            data_dir, proxy=args.proxy or None, progress_cb=progress):
        raise SystemExit("CosyVoice2 模型下载失败")
    ok, reason = runtime_smoke(data_dir, deep=True)
    if not ok:
        raise SystemExit(f"CosyVoice 模型加载验证失败：{reason}")
    print(json.dumps(setup_status(data_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
