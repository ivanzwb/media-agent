"""Offline license verification (Ed25519).

A license is a JSON *payload* signed by the vendor's Ed25519 private key. The
app ships only the matching PUBLIC key, so licenses cannot be forged without
the private key. The activation string is:

    base64url( json({"payload": <payload>, "sig": <base64 signature>}) )

Public key resolution order:
  1. env MEDIA_AGENT_LICENSE_PUBKEY  (base64 of the 32-byte raw key)
  2. app/licensing/public_key.b64    (committed)
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def canonical(payload: dict) -> bytes:
    """Deterministic bytes for signing/verifying a payload."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _public_key_b64() -> str:
    env = os.environ.get("MEDIA_AGENT_LICENSE_PUBKEY")
    if env and env.strip():
        return env.strip()
    p = Path(__file__).with_name("public_key.b64")
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    return ""


def _b64decode(s: str) -> bytes:
    s = s.strip()
    pad = "=" * (-len(s) % 4)
    try:
        return base64.urlsafe_b64decode(s + pad)
    except Exception:                           # noqa: BLE001
        return base64.b64decode(s + pad)


def verify_payload(payload: dict, sig_b64: str) -> bool:
    """True if `sig_b64` is a valid vendor signature over `payload`."""
    pub_b64 = _public_key_b64()
    if not pub_b64:
        return False
    try:
        pub = Ed25519PublicKey.from_public_bytes(_b64decode(pub_b64))
        pub.verify(_b64decode(sig_b64), canonical(payload))
        return True
    except (InvalidSignature, ValueError, Exception):   # noqa: BLE001
        return False


def parse_activation(blob: str) -> tuple[dict | None, str]:
    """Decode an activation string -> (payload, signature_b64).

    Returns (None, "") if it can't be decoded.
    """
    try:
        raw = _b64decode(blob)
        obj = json.loads(raw.decode("utf-8"))
        payload = obj.get("payload")
        sig = obj.get("sig", "")
        if isinstance(payload, dict) and isinstance(sig, str):
            return payload, sig
    except Exception:                           # noqa: BLE001
        pass
    return None, ""


def encode_activation(payload: dict, sig_b64: str) -> str:
    """Vendor-side: pack a signed payload into an activation string."""
    obj = {"payload": payload, "sig": sig_b64}
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")
