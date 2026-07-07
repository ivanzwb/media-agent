"""LicenseManager — loads/validates/activates the local license.

Offline model: no server. Unactivated == free tier (issue #27 decision). A
dev bypass env (MEDIA_AGENT_LICENSE_DEV=1) unlocks everything for the developer
so local runs are never blocked; it defaults OFF for shipped builds.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

from app.licensing import features as F
from app.licensing import store as lstore
from app.licensing import verify as lverify
from app.licensing.fingerprint import machine_id, machine_code

_ROLLBACK_TOLERANCE = 24 * 3600  # allow 1 day of backwards clock drift


def _dev_bypass() -> bool:
    return os.environ.get("MEDIA_AGENT_LICENSE_DEV", "").strip().lower() in (
        "1", "true", "yes")


def _parse_dt(s) -> datetime | None:
    if not s:
        return None
    try:
        txt = str(s).replace("Z", "+00:00")
        dt = datetime.fromisoformat(txt)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class LicenseManager:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.edition = "free"
        self.features: set[str] = set()
        self.expires_at: datetime | None = None
        self.key = ""
        self.reason = "未激活（免费版）"
        self._dev = False
        self.reload()

    # ── loading / validation ────────────────────────────────────────────
    def reload(self) -> None:
        self._reset()
        if _dev_bypass():
            self._dev = True
            self.edition = "pro"
            self.features = set(F.PRO_FEATURES)
            self.reason = "开发者模式（MEDIA_AGENT_LICENSE_DEV）"
            return
        # Tamper check: a swapped public key invalidates all licenses.
        from app.licensing import integrity
        if not integrity.public_key_ok():
            self.reason = "授权组件完整性校验失败"
            return
        blob = lstore.load_blob(self.data_dir)
        if not blob:
            return
        payload, sig = lverify.parse_activation(blob)
        if not payload or not lverify.verify_payload(payload, sig):
            self.reason = "授权文件无效（签名校验失败）"
            return
        # machine binding
        bound = payload.get("machine_id")
        if bound and bound != machine_id()[:len(bound)]:
            self.reason = "授权绑定到其他机器"
            return
        # clock rollback check
        now = time.time()
        last = lstore.read_last_seen(self.data_dir)
        if last and now < last - _ROLLBACK_TOLERANCE:
            self.reason = "系统时间异常（疑似回拨），授权暂不可用"
            return
        # expiry
        exp = _parse_dt(payload.get("expires_at"))
        if exp and datetime.now(timezone.utc) > exp:
            self.reason = f"授权已于 {exp.date()} 过期"
            self.expires_at = exp
            return
        # valid
        self.edition = str(payload.get("edition", "pro"))
        feats = payload.get("features") or list(F.PRO_FEATURES)
        self.features = set(feats)
        self.expires_at = exp
        self.key = str(payload.get("key", ""))
        self.reason = "已激活"
        lstore.write_last_seen(self.data_dir, now)

    def _reset(self) -> None:
        self.edition = "free"
        self.features = set()
        self.expires_at = None
        self.key = ""
        self.reason = "未激活（免费版）"
        self._dev = False

    # ── queries ──────────────────────────────────────────────────────────
    @property
    def active(self) -> bool:
        return self._dev or self.edition == "pro" and bool(self.features)

    def has_feature(self, feature: str) -> bool:
        if self._dev:
            return True
        return self.active and feature in self.features

    def status_dict(self) -> dict:
        return {
            "active": self.active,
            "edition": self.edition,
            "dev": self._dev,
            "features": sorted(self.features),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "reason": self.reason,
            "key": self.key,
            "machine_code": machine_code(),
        }

    # ── activation ─────────────────────────────────────────────────────────
    def activate(self, activation: str) -> tuple[bool, str]:
        payload, sig = lverify.parse_activation((activation or "").strip())
        if not payload or not lverify.verify_payload(payload, sig):
            return False, "激活码无效或签名校验失败"
        bound = payload.get("machine_id")
        if bound and bound != machine_id()[:len(bound)]:
            return False, "该激活码绑定到其他机器，无法在本机使用"
        exp = _parse_dt(payload.get("expires_at"))
        if exp and datetime.now(timezone.utc) > exp:
            return False, f"该激活码已于 {exp.date()} 过期"
        lstore.save_blob(self.data_dir, lverify.encode_activation(payload, sig))
        lstore.write_last_seen(self.data_dir)
        self.reload()
        return (True, "激活成功") if self.active else (False, self.reason)

    def deactivate(self) -> None:
        lstore.clear_blob(self.data_dir)
        self.reload()
