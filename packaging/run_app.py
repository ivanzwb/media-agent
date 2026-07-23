"""Frozen-app entry point (used by the PyInstaller build).

Supports special CLI flags for the setup script (see setup-optional.bat):

  --check-module NAME     Import NAME and exit 0 on success, 1 on failure.
  --run-module  NAME ...  Run python -m NAME with remaining args.

Without flags, starts the local web UI and opens the browser.
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


def _find_internal_dir() -> Path | None:
    """Return the _internal dir next to the frozen executable, if any."""
    if not getattr(sys, "frozen", False):
        return None
    internal = Path(sys.executable).resolve().parent / "_internal"
    return internal if internal.is_dir() else None


def _check_module(name: str) -> int:
    """Try importing *name*; return 0 on success, 1 on failure."""
    try:
        import importlib

        importlib.import_module(name)
        return 0
    except ImportError:
        import traceback

        traceback.print_exc()
        return 1


def _run_module() -> int:
    """Run a module (python -m equivalent) inside the frozen environment.

    Usage: --run-module <module> [args...]
    """
    if len(sys.argv) < 3:
        print("Usage: --run-module <module> [args...]", file=sys.stderr)
        return 1
    module = sys.argv[2]
    sys.argv = sys.argv[2:]  # replace argv so the module sees its own args
    import runpy

    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    except Exception as e:
        print(f"Error running module '{module}': {e}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    # Handle special CLI flags used by setup-optional.bat -----------------
    if len(sys.argv) > 1:
        if sys.argv[1] == "--check-module" and len(sys.argv) > 2:
            sys.exit(_check_module(sys.argv[2]))
        if sys.argv[1] == "--run-module":
            sys.exit(_run_module())

    # Normal startup ------------------------------------------------------
    os.environ.setdefault("MEDIA_AGENT_DATA_DIR", _default_data_dir())
    import uvicorn
    from app.config import Config
    from app.feeds import ensure_feeds_file
    from app.web.server import create_app

    cfg = Config.load()
    cfg.ensure_dirs()
    # Keep feeds.yaml in the (portable) data dir so sources added via the UI
    # persist next to the exe. Create a default one on first run so the app is
    # usable out of the box.
    feeds_path = str(cfg.data_dir / "feeds.yaml")
    ensure_feeds_file(feeds_path)
    app = create_app(cfg, feeds_path=feeds_path)
    host, port = "127.0.0.1", 8000
    threading.Timer(1.5, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
