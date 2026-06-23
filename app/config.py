from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.store import Store


def _int_env(name: str) -> int | None:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return None
    try:
        return int(val)
    except ValueError:
        return None


def _int_str(val: str | None) -> int | None:
    """Parse a string from DB into int, returning None on empty/invalid."""
    if val is None or val.strip() == "":
        return None
    try:
        return int(val)
    except ValueError:
        return None


@dataclass
class Config:
    data_dir: Path
    llm_provider: str = "mock"
    llm_api_key: str | None = None
    llm_model: str | None = None
    image_provider: str | None = None
    max_age_days: int | None = None
    max_per_source: int | None = None

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    @property
    def drafts_dir(self) -> Path:
        return self.data_dir / "drafts"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "media.db"

    @classmethod
    def load(cls, store: "Store | None" = None) -> "Config":
        data_dir = Path(os.environ.get("MEDIA_AGENT_DATA_DIR", "data")).resolve()
        config = cls(
            data_dir=data_dir,
            llm_provider=os.environ.get("MEDIA_AGENT_LLM_PROVIDER", "mock"),
            llm_api_key=os.environ.get("MEDIA_AGENT_LLM_API_KEY"),
            llm_model=os.environ.get("MEDIA_AGENT_LLM_MODEL"),
            image_provider=os.environ.get("MEDIA_AGENT_IMAGE_PROVIDER"),
            max_age_days=_int_env("MEDIA_AGENT_MAX_AGE_DAYS"),
            max_per_source=_int_env("MEDIA_AGENT_MAX_PER_SOURCE"),
        )
        if store is not None:
            config._apply_db_overrides(store)
        return config

    def _apply_db_overrides(self, store: "Store") -> None:
        """Apply settings stored in DB, overriding env var values."""
        str_overrides = {
            "llm_provider": "llm_provider",
            "llm_api_key": "llm_api_key",
            "llm_model": "llm_model",
            "image_provider": "image_provider",
        }
        for attr, db_key in str_overrides.items():
            val = store.get_setting(db_key)
            if val is not None and val.strip() != "":
                setattr(self, attr, val)

        # Integer fields
        age = store.get_setting("max_age_days")
        if age is not None:
            parsed = _int_str(age)
            if parsed is not None:
                self.max_age_days = parsed

        per_src = store.get_setting("max_per_source")
        if per_src is not None:
            parsed = _int_str(per_src)
            if parsed is not None:
                self.max_per_source = parsed

    def ensure_dirs(self) -> None:
        for d in (self.archive_dir, self.drafts_dir, self.images_dir):
            d.mkdir(parents=True, exist_ok=True)
