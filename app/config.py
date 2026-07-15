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
    llm_api_base: str | None = None
    image_provider: str | None = None
    image_api_key: str | None = None
    image_api_base: str | None = None
    image_model: str | None = None
    tts_provider: str | None = None
    tts_api_base: str | None = None
    tts_api_key: str | None = None
    tts_model: str | None = None
    tts_voice: str | None = None
    max_age_days: int | None = None
    max_per_source: int | None = None
    download_workers: int | None = None
    video_fit: str | None = None  # fit | crop | blur
    video_brand_name: str | None = None  # brand name for intro/outro
    sensitive_level: str | None = None  # off | basic | standard | strict
    sensitive_words: str | None = None  # user custom list (comma/newline)
    promotion_footer: str | None = None  # promotion footer appended to drafts
    wechat_appid: str | None = None      # WeChat Official Account AppID
    wechat_appsecret: str | None = None  # WeChat Official Account AppSecret
    wechat_author: str | None = None     # default author shown on published 图文
    rewrite_style: str | None = None     # default rewrite style id (see app.pipeline.styles)
    download_images: bool = True          # download article images to local
    download_videos: bool = True          # download article videos to local
    cli_tool: str | None = None          # "auto" | "opencode" | "codex" | "copilot" | "none"
    rewrite_priority: str = "agent"      # "agent" | "llm" — which backend wins when both are set
    cli_timeout: int = 500               # CLI agent timeout in seconds
    fetch_proxy: str | None = None       # HTTP proxy for RSS/scraper fetches (e.g. http://127.0.0.1:7890)

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
    def videos_dir(self) -> Path:
        return self.data_dir / "videos"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def voices_dir(self) -> Path:
        return self.data_dir / "voices"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "media.db"

    @property
    def workers(self) -> int:
        """Concurrency for downloads/fetching (default: CPU count)."""
        if self.download_workers and self.download_workers > 0:
            return self.download_workers
        return os.cpu_count() or 4

    @classmethod
    def load(cls, store: "Store | None" = None) -> "Config":
        data_dir = Path(os.environ.get("MEDIA_AGENT_DATA_DIR", "data")).resolve()
        config = cls(
            data_dir=data_dir,
            llm_provider=os.environ.get("MEDIA_AGENT_LLM_PROVIDER", "mock"),
            llm_api_key=os.environ.get("MEDIA_AGENT_LLM_API_KEY"),
            llm_model=os.environ.get("MEDIA_AGENT_LLM_MODEL"),
            llm_api_base=os.environ.get("MEDIA_AGENT_LLM_API_BASE"),
            image_provider=os.environ.get("MEDIA_AGENT_IMAGE_PROVIDER"),
            image_api_key=os.environ.get("MEDIA_AGENT_IMAGE_API_KEY"),
            image_api_base=os.environ.get("MEDIA_AGENT_IMAGE_API_BASE"),
            image_model=os.environ.get("MEDIA_AGENT_IMAGE_MODEL"),
            tts_provider=os.environ.get("MEDIA_AGENT_TTS_PROVIDER"),
            tts_api_base=os.environ.get("MEDIA_AGENT_TTS_API_BASE"),
            tts_api_key=os.environ.get("MEDIA_AGENT_TTS_API_KEY"),
            tts_model=os.environ.get("MEDIA_AGENT_TTS_MODEL"),
            tts_voice=os.environ.get("MEDIA_AGENT_TTS_VOICE"),
            max_age_days=_int_env("MEDIA_AGENT_MAX_AGE_DAYS"),
            max_per_source=_int_env("MEDIA_AGENT_MAX_PER_SOURCE"),
            download_workers=_int_env("MEDIA_AGENT_DOWNLOAD_WORKERS"),
            video_fit=os.environ.get("MEDIA_AGENT_VIDEO_FIT_MODE"),
            video_brand_name=os.environ.get("MEDIA_AGENT_VIDEO_BRAND_NAME"),
            sensitive_level=os.environ.get("MEDIA_AGENT_SENSITIVE_LEVEL"),
            sensitive_words=os.environ.get("MEDIA_AGENT_SENSITIVE_WORDS"),
            promotion_footer=os.environ.get("MEDIA_AGENT_PROMOTION_FOOTER"),
            wechat_appid=os.environ.get("MEDIA_AGENT_WECHAT_APPID"),
            wechat_appsecret=os.environ.get("MEDIA_AGENT_WECHAT_APPSECRET"),
            wechat_author=os.environ.get("MEDIA_AGENT_WECHAT_AUTHOR"),
            rewrite_style=os.environ.get("MEDIA_AGENT_REWRITE_STYLE"),
            download_images=os.environ.get("MEDIA_AGENT_DOWNLOAD_IMAGES", "1") not in ("0", "false", "no"),
            download_videos=os.environ.get("MEDIA_AGENT_DOWNLOAD_VIDEOS", "1") not in ("0", "false", "no"),
            cli_tool=os.environ.get("MEDIA_AGENT_CLI_TOOL", "none"),
            rewrite_priority=os.environ.get("MEDIA_AGENT_REWRITE_PRIORITY", "agent"),
            cli_timeout=_int_env("MEDIA_AGENT_CLI_TIMEOUT") or 500,
            fetch_proxy=os.environ.get("MEDIA_AGENT_FETCH_PROXY"),
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
            "llm_api_base": "llm_api_base",
            "image_provider": "image_provider",
            "image_api_key": "image_api_key",
            "image_api_base": "image_api_base",
            "image_model": "image_model",
            "tts_provider": "tts_provider",
            "tts_api_base": "tts_api_base",
            "tts_api_key": "tts_api_key",
            "tts_model": "tts_model",
            "tts_voice": "tts_voice",
            "video_fit": "video_fit",
            "video_brand_name": "video_brand_name",
            "sensitive_level": "sensitive_level",
            "sensitive_words": "sensitive_words",
            "promotion_footer": "promotion_footer",
            "wechat_appid": "wechat_appid",
            "wechat_appsecret": "wechat_appsecret",
            "wechat_author": "wechat_author",
            "rewrite_style": "rewrite_style",
            "cli_tool": "cli_tool",
            "rewrite_priority": "rewrite_priority",
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

        workers = store.get_setting("download_workers")
        if workers is not None:
            parsed = _int_str(workers)
            if parsed is not None:
                self.download_workers = parsed

        cli_to = store.get_setting("cli_timeout")
        if cli_to is not None:
            parsed = _int_str(cli_to)
            if parsed is not None:
                self.cli_timeout = parsed

        # Boolean fields
        di = store.get_setting("download_images")
        if di is not None:
            self.download_images = di.strip() not in ("0", "false", "no", "")
        dv = store.get_setting("download_videos")
        if dv is not None:
            self.download_videos = dv.strip() not in ("0", "false", "no", "")

    def ensure_dirs(self) -> None:
        for d in (self.archive_dir, self.drafts_dir, self.images_dir,
                  self.videos_dir, self.media_dir, self.voices_dir):
            d.mkdir(parents=True, exist_ok=True)
