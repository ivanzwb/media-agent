"""Minimal tamper self-check.

Threat: an attacker swaps ``public_key.b64`` with their own key to mint valid
licenses. Defense: pin the SHA-256 of the trusted public key. If the key in use
doesn't match the pin, licenses are rejected (app falls back to free tier).

``EXPECTED_PUBKEY_SHA256`` is empty by default (repo/dev = no pinning). Running
``python -m tools.licctl keygen`` writes the vendor's key AND sets this pin, so
distributed builds are pinned. For stronger hardening, compile this module +
verify.py/manager.py with Cython/pyarmor (see tools/build_hardened.py).
"""
from __future__ import annotations

import hashlib

# Set automatically by `licctl keygen`. Empty => not pinned (dev/repo default).
EXPECTED_PUBKEY_SHA256 = ""


def public_key_ok() -> bool:
    """True if the public key in use matches the pinned hash (or not pinned)."""
    if not EXPECTED_PUBKEY_SHA256:
        return True
    from app.licensing.verify import _public_key_b64
    pk = _public_key_b64()
    if not pk:
        return False
    return hashlib.sha256(pk.encode("utf-8")).hexdigest() == EXPECTED_PUBKEY_SHA256
