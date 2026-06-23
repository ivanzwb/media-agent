from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int_env(name: str) -> int | None:
    val = os.environ.get(name)
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
    def load(cls) -> "Config":
        data_dir = Path(os.environ.get("MEDIA_AGENT_DATA_DIR", "data")).resolve()
        return cls(
            data_dir=data_dir,
            llm_provider=os.environ.get("MEDIA_AGENT_LLM_PROVIDER", "mock"),
            llm_api_key=os.environ.get("MEDIA_AGENT_LLM_API_KEY"),
            llm_model=os.environ.get("MEDIA_AGENT_LLM_MODEL"),
            image_provider=os.environ.get("MEDIA_AGENT_IMAGE_PROVIDER"),
            max_age_days=_int_env("MEDIA_AGENT_MAX_AGE_DAYS"),
            max_per_source=_int_env("MEDIA_AGENT_MAX_PER_SOURCE"),
        )

    def ensure_dirs(self) -> None:
        for d in (self.archive_dir, self.drafts_dir, self.images_dir):
            d.mkdir(parents=True, exist_ok=True)
