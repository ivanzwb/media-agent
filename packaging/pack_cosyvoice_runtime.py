"""Pack and split a platform-specific CosyVoice runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import conda_pack

ASSET_NAME = "cosyvoice-runtime-win64-cuda121.tar.gz"
MANIFEST_NAME = "cosyvoice-runtime-win64-cuda121.manifest.json"
PART_BYTES = 1800 * 1024 * 1024
COPY_BYTES = 8 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(COPY_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def pack_runtime(prefix: Path, output_dir: Path, *, worker: Path,
                 asset_name: str = ASSET_NAME,
                 keep_archive: bool = False) -> tuple[Path, list[Path]]:
    prefix = prefix.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(worker.resolve(), prefix / "cosyvoice-worker.py")
    archive = output_dir / asset_name
    conda_pack.pack(
        prefix=str(prefix), output=str(archive.resolve()), format="tar.gz",
        compress_level=6, force=True, ignore_missing_files=True)
    parts: list[dict[str, object]] = []
    paths: list[Path] = []
    with archive.open("rb") as source:
        index = 1
        while True:
            destination = output_dir / f"{asset_name}.part{index:03d}"
            written = 0
            digest = hashlib.sha256()
            with destination.open("wb") as output:
                while written < PART_BYTES:
                    chunk = source.read(min(COPY_BYTES, PART_BYTES - written))
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
            if not written:
                destination.unlink(missing_ok=True)
                break
            parts.append({"name": destination.name, "size": written,
                          "sha256": digest.hexdigest()})
            paths.append(destination)
            index += 1
    manifest = {
        "version": 1, "archive": asset_name,
        "archive_size": archive.stat().st_size,
        "archive_sha256": _sha256(archive), "parts": parts,
    }
    manifest_name = asset_name.removesuffix(".tar.gz") + ".manifest.json"
    manifest_path = output_dir / manifest_name
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if not keep_archive:
        archive.unlink()
    return manifest_path, paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", required=True, type=Path)
    parser.add_argument("--output-dir", default=Path.cwd(), type=Path)
    parser.add_argument(
        "--worker", default=Path(__file__).with_name("cosyvoice_worker.py"),
        type=Path)
    parser.add_argument("--keep-archive", action="store_true")
    parser.add_argument("--asset-name", default=ASSET_NAME)
    args = parser.parse_args()
    manifest, parts = pack_runtime(
        args.prefix, args.output_dir, worker=args.worker,
        asset_name=args.asset_name,
        keep_archive=args.keep_archive)
    print(manifest)
    for part in parts:
        print(part)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
