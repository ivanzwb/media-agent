"""Encrypted-at-rest license storage, bound to the machine fingerprint.

The activation string is encrypted with a Fernet key *derived from the machine
id*, so copying ``license.bin`` to another machine makes it undecryptable
there (in addition to the payload's own machine_id check). A tiny companion
state file records the last-seen time to detect clock rollback.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.licensing.fingerprint import machine_id

_LICENSE_FILE = "license.bin"
_STATE_FILE = ".licstate"


def _fernet() -> Fernet:
    key = hashlib.sha256(("media-agent-lic:" + machine_id()).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def save_blob(data_dir: Path, blob: str) -> None:
    f = _fernet()
    (Path(data_dir) / _LICENSE_FILE).write_bytes(f.encrypt(blob.encode("utf-8")))


def load_blob(data_dir: Path) -> str | None:
    p = Path(data_dir) / _LICENSE_FILE
    if not p.exists():
        return None
    try:
        return _fernet().decrypt(p.read_bytes()).decode("utf-8")
    except (InvalidToken, ValueError, Exception):   # noqa: BLE001
        return None            # wrong machine / corrupted → treat as unlicensed


def clear_blob(data_dir: Path) -> None:
    for name in (_LICENSE_FILE, _STATE_FILE):
        try:
            (Path(data_dir) / name).unlink()
        except OSError:
            pass


# ── anti clock-rollback ──────────────────────────────────────────────────────
def read_last_seen(data_dir: Path) -> float:
    p = Path(data_dir) / _STATE_FILE
    if not p.exists():
        return 0.0
    try:
        obj = json.loads(_fernet().decrypt(p.read_bytes()).decode("utf-8"))
        return float(obj.get("last_seen", 0.0))
    except Exception:                           # noqa: BLE001
        return 0.0


def write_last_seen(data_dir: Path, ts: float | None = None) -> None:
    ts = time.time() if ts is None else ts
    try:
        blob = json.dumps({"last_seen": ts}).encode("utf-8")
        (Path(data_dir) / _STATE_FILE).write_bytes(_fernet().encrypt(blob))
    except Exception:                           # noqa: BLE001
        pass
