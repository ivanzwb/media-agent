"""Runtime capability reporting used by Settings and post-install refreshes."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


def get_capabilities(data_dir: Path) -> dict[str, Any]:
    from app.cosyvoice_install import setup_status as cosyvoice_status
    from app.video.sadtalker_setup import setup_status as sadtalker_status

    return {
        "cosyvoice": cosyvoice_status(data_dir),
        "sadtalker": sadtalker_status(data_dir),
        "ffmpeg": {"ready": bool(shutil.which("ffmpeg"))},
    }
