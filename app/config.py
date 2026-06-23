from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    data_dir: Path
    llm_provider: str = "mock"
    llm_api_key: str | None = None
    llm_model: str | None = None
    image_provider: str | None = None

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
        )

    def ensure_dirs(self) -> None:
        for d in (self.archive_dir, self.drafts_dir, self.images_dir):
            d.mkdir(parents=True, exist_ok=True)
