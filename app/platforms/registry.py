"""Platform registry — auto-discovers Platform subclasses in this package."""

from __future__ import annotations

from app.platforms import Platform

_ALL: dict[str, Platform] = {}


def register(cls: type[Platform]) -> None:
    """Register a Platform subclass. Called by each platform module."""
    inst = cls()
    _ALL[inst.id] = inst


def get(id: str) -> Platform | None:            # noqa: A002
    """Get a platform by its id (e.g. 'wechat')."""
    return _ALL.get(id)


def list_all() -> list[Platform]:
    """Return all registered platforms, insertion-order."""
    return list(_ALL.values())


# Import platform modules at bottom so they can import register from here.
from app.platforms import wechat      # noqa: E402, F401  # isort: skip
from app.platforms import xiaohongshu  # noqa: E402, F401  # isort: skip
from app.platforms import zhihu       # noqa: E402, F401  # isort: skip
from app.platforms import toutiao     # noqa: E402, F401  # isort: skip
