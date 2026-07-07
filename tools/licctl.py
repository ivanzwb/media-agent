"""Vendor-side license tool (offline Ed25519).

Keep the PRIVATE key secret (never commit). Commit only the PUBLIC key
(app/licensing/public_key.b64), which the app uses to verify licenses.

Usage:
  # 1. one-time: generate a keypair
  python -m tools.licctl keygen
      -> writes tools/license_private_key.b64 (SECRET, gitignored)
         and    app/licensing/public_key.b64  (commit this)

  # 2. issue a license (prints the activation code to give the buyer)
  python -m tools.licctl issue --key MA-PRO-0001 --days 365
  python -m tools.licctl issue --key MA-PRO-0002 --machine 1A2B-3C4D-5E6F-7A8B
  python -m tools.licctl issue --key MA-PRO-0003          # perpetual, floating
"""
from __future__ import annotations

import argparse
import base64
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)

_ROOT = Path(__file__).resolve().parent.parent
_PRIV = _ROOT / "tools" / "license_private_key.b64"
_PUB = _ROOT / "app" / "licensing" / "public_key.b64"

# import the app's canonical encoders so signing matches verification exactly
sys.path.insert(0, str(_ROOT))
from app.licensing import features as F           # noqa: E402
from app.licensing.verify import canonical, encode_activation  # noqa: E402


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def keygen() -> None:
    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption())
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    pub_str = _b64(pub_raw)
    _PRIV.write_text(_b64(priv_raw), encoding="utf-8")
    _PUB.write_text(pub_str, encoding="utf-8")
    # Pin the public key hash for the tamper self-check (integrity.py).
    import hashlib
    import re
    h = hashlib.sha256(pub_str.encode("utf-8")).hexdigest()
    integ = _ROOT / "app" / "licensing" / "integrity.py"
    txt = integ.read_text(encoding="utf-8")
    txt = re.sub(r'EXPECTED_PUBKEY_SHA256 = "[^"]*"',
                 f'EXPECTED_PUBKEY_SHA256 = "{h}"', txt)
    integ.write_text(txt, encoding="utf-8")
    print(f"private key -> {_PRIV}  (SECRET — do not commit)")
    print(f"public  key -> {_PUB}   (commit this)")
    print(f"pinned hash -> app/licensing/integrity.py (commit this)")


def _load_priv() -> Ed25519PrivateKey:
    if not _PRIV.exists():
        sys.exit("no private key; run `python -m tools.licctl keygen` first")
    raw = base64.urlsafe_b64decode(_PRIV.read_text().strip() + "==")
    return Ed25519PrivateKey.from_private_bytes(raw)


def issue(key: str, days: int | None, machine: str | None,
          features: list[str]) -> None:
    priv = _load_priv()
    now = datetime.now(timezone.utc)
    payload = {
        "key": key,
        "edition": "pro",
        "features": features or list(F.PRO_FEATURES),
        "machine_id": (machine or "").replace("-", "").lower() or None,
        "issued_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "expires_at": (
            (now + timedelta(days=days)).replace(microsecond=0)
            .isoformat().replace("+00:00", "Z") if days else None),
    }
    sig = priv.sign(canonical(payload))
    activation = encode_activation(payload, base64.b64encode(sig).decode())
    print("\n=== 激活码（发给购买者，在设置页粘贴激活）===\n")
    print(activation)
    print("\n=== license payload ===")
    print(payload)


def main() -> None:
    ap = argparse.ArgumentParser(description="media-agent license tool")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("keygen")
    p = sub.add_parser("issue")
    p.add_argument("--key", required=True, help="human-readable order code")
    p.add_argument("--days", type=int, default=None,
                   help="validity in days (omit = perpetual)")
    p.add_argument("--machine", default=None,
                   help="bind to a machine code (omit = any machine)")
    p.add_argument("--features", default=None,
                   help="comma list; default = all Pro features")
    args = ap.parse_args()
    if args.cmd == "keygen":
        keygen()
    elif args.cmd == "issue":
        feats = ([s.strip() for s in args.features.split(",") if s.strip()]
                 if args.features else None)
        issue(args.key, args.days, args.machine, feats)


if __name__ == "__main__":
    main()
