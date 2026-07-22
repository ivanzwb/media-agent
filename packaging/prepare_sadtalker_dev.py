"""Install a locally packed SadTalker runtime into development data paths."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.video.sadtalker_setup import (
    download_models,
    gpu_status,
    install_runtime_archive,
    runtime_files_ready,
    runtime_smoke,
    setup_status,
)


def _progress(message: str, fraction: float) -> None:
    percent = "" if fraction < 0 else f" [{fraction * 100:5.1f}%]"
    print(f"{message}{percent}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--data-dir", default=Path("data"), type=Path)
    parser.add_argument("--proxy", default="")
    args = parser.parse_args()
    data_dir = args.data_dir.resolve()

    if not runtime_files_ready(data_dir):
        install_runtime_archive(
            data_dir, args.archive, progress_cb=_progress,
            cleanup_archive=False)
    else:
        print("SadTalker CUDA runtime 已安装，跳过解压。", flush=True)
    if not download_models(
        data_dir, proxy=args.proxy or None, progress_cb=_progress):
        raise SystemExit("SadTalker 模型下载失败")

    gpu = gpu_status()
    if gpu["ok"]:
        print("运行真实 GPU 推理验证…", flush=True)
        ok, reason = runtime_smoke(data_dir, deep=True)
        if not ok:
            raise SystemExit(f"SadTalker 推理验证失败：{reason}")
    else:
        print(f"跳过 GPU 推理验证：{gpu['reason']}", flush=True)

    print(json.dumps(setup_status(data_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
