"""Feature-gate helpers used by web routes / pipeline.

`require(...)` returns a ready 403 JSONResponse when a Pro feature is locked
(else None). The free-tier daily rewrite quota lives in the DB settings table.
"""
from __future__ import annotations

from datetime import date

from app.licensing import features as F


def feature_error(feature: str):
    from fastapi.responses import JSONResponse
    return JSONResponse(
        {"ok": False, "upgrade": True,
         "error": f"「{F.label(feature)}」为 Pro 版功能，激活后可用。"},
        status_code=403)


def require(manager, feature: str):
    """None if licensed, else a 403 JSONResponse."""
    return None if manager.has_feature(feature) else feature_error(feature)


# ── free-tier daily rewrite quota ───────────────────────────────────────────
def _today_key() -> str:
    return "rewrite_used_" + date.today().strftime("%Y%m%d")


def rewrite_remaining(manager, store) -> int | None:
    """None => unlimited (Pro); else remaining free rewrites today."""
    if manager.has_feature(F.REWRITE_UNLIMITED):
        return None
    try:
        used = int(store.get_setting(_today_key(), "0") or "0")
    except (TypeError, ValueError):
        used = 0
    return max(0, F.FREE_REWRITE_PER_DAY - used)


def rewrite_allowed(manager, store) -> bool:
    rem = rewrite_remaining(manager, store)
    return rem is None or rem > 0


def consume_rewrite(manager, store) -> None:
    if manager.has_feature(F.REWRITE_UNLIMITED):
        return
    try:
        used = int(store.get_setting(_today_key(), "0") or "0")
    except (TypeError, ValueError):
        used = 0
    store.set_setting(_today_key(), str(used + 1))


def rewrite_error():
    from fastapi.responses import JSONResponse
    return JSONResponse(
        {"ok": False, "upgrade": True,
         "error": f"免费版每日仅可改写 {F.FREE_REWRITE_PER_DAY} 篇，今日额度已用完；"
                  "升级 Pro 版可无限改写。"},
        status_code=403)
