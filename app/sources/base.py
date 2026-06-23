from __future__ import annotations

from typing import Protocol

from app.models import Article


class Source(Protocol):
    name: str

    def fetch(self) -> list[Article]:
        ...
