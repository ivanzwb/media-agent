"""Stable per-machine fingerprint used to bind a license to one machine."""
from __future__ import annotations

import hashlib
import platform
import uuid


def machine_id() -> str:
    """A stable, non-reversible machine id (hex).

    Derived from the primary MAC address (uuid.getnode) + hostname. Not a
    secret — it's shown to the user as their 机器码 so a vendor can issue a
    machine-locked license.
    """
    parts = [str(uuid.getnode()), platform.node() or "", platform.system() or ""]
    raw = "|".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def machine_code() -> str:
    """Human-friendly grouping of the machine id for display / copy."""
    mid = machine_id()
    return "-".join(mid[i:i + 4] for i in range(0, 16, 4)).upper()
