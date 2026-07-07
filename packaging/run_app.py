"""Frozen-app entry point (used by the PyInstaller build).

Starts the local web UI and opens the browser. Data lives next to the exe
unless MEDIA_AGENT_DATA_DIR is set.
"""
from __future__ import annotations

import os
import sys
import threading
import webbrowser
from pathlib import Path


def _default_data_dir() -> str:
    # When frozen, put data beside the executable for a portable install.
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve().parent / "data")
    return os.environ.get("MEDIA_AGENT_DATA_DIR", "data")


def main() -> None:
    os.environ.setdefault("MEDIA_AGENT_DATA_DIR", _default_data_dir())
    import uvicorn
    from app.config import Config
    from app.web.server import create_app

    cfg = Config.load()
    cfg.ensure_dirs()
    app = create_app(cfg)
    host, port = "127.0.0.1", 8000
    threading.Timer(1.5, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
