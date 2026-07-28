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
    tts_rate: str | None = None    # 语速, e.g. "+0%" / "-10%" / "+20%" (ffmpeg atempo)
    tts_pitch: str | None = None   # 音调, e.g. "+0Hz" / "+15Hz" / "-10Hz" (ffmpeg pitch)
    tts_instruct: str | None = None  # CosyVoice 语气/情感指令, e.g. "用亲切自然的语气"
    max_age_days: int | None = None
    max_per_source: int | None = None
    max_drafts: int | None = None   # 每次运行最多改写/转写的文章篇数 (None → 默认 10)
    download_workers: int | None = None
    video_fit: str | None = None  # fit | crop | blur
    video_brand_name: str | None = None  # brand name for intro/outro
    # 数字人主播 (digital-human presenter)
    avatar_enabled: bool = False          # composite a presenter into the video
    avatar_image: str | None = None       # presenter portrait filename under data/avatar/
    avatar_position: str | None = None    # default placement: pip | full
    avatar_provider: str | None = None    # sadtalker | still
    sadtalker_dir: str | None = None      # path to a local SadTalker checkout
    sadtalker_python: str | None = None   # python exe to run SadTalker (optional)
    sensitive_level: str | None = None  # off | basic | standard | strict
    sensitive_words: str | None = None  # user custom list (comma/newline)
    promotion_footer: str | None = None  # promotion footer appended to drafts
    seo_tags_enabled: bool = True        # generate article SEO tags before promotion
    wechat_appid: str | None = None      # WeChat Official Account AppID
    wechat_appsecret: str | None = None  # WeChat Official Account AppSecret
    wechat_author: str | None = None     # default author shown on published 图文
    rewrite_style: str | None = None     # default rewrite style id (see app.pipeline.styles)
    download_images: bool = True          # download article images to local
    download_videos: bool = True          # download article videos to local
    relevance_filter: bool = True         # LLM gate: keep only news/industry/research articles
    cli_tool: str | None = None          # "auto" | "opencode" | "codex" | "copilot" | "none"
    rewrite_priority: str = "agent"      # "agent" | "llm" — which backend wins when both are set
    cli_timeout: int = 500               # CLI agent timeout in seconds
    llm_timeout: int = 120               # OpenAI-compatible API timeout in seconds
    fetch_proxy: str | None = None       # HTTP proxy for RSS/scraper fetches (e.g. http://127.0.0.1:7890)
    github_mirror: str | None = None     # GitHub 镜像前缀
    huggingface_mirror: str | None = None  # HuggingFace 镜像前缀 (HF_ENDPOINT)

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
    def avatar_dir(self) -> Path:
        return self.data_dir / "avatar"

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
            tts_rate=os.environ.get("MEDIA_AGENT_TTS_RATE"),
            tts_pitch=os.environ.get("MEDIA_AGENT_TTS_PITCH"),
            tts_instruct=os.environ.get("MEDIA_AGENT_TTS_INSTRUCT"),
            max_age_days=_int_env("MEDIA_AGENT_MAX_AGE_DAYS"),
            max_per_source=_int_env("MEDIA_AGENT_MAX_PER_SOURCE"),
            max_drafts=_int_env("MEDIA_AGENT_MAX_DRAFTS"),
            download_workers=_int_env("MEDIA_AGENT_DOWNLOAD_WORKERS"),
            video_fit=os.environ.get("MEDIA_AGENT_VIDEO_FIT_MODE"),
            video_brand_name=os.environ.get("MEDIA_AGENT_VIDEO_BRAND_NAME"),
            avatar_enabled=os.environ.get("MEDIA_AGENT_AVATAR_ENABLED", "0") not in ("0", "false", "no", ""),
            avatar_image=os.environ.get("MEDIA_AGENT_AVATAR_IMAGE"),
            avatar_position=os.environ.get("MEDIA_AGENT_AVATAR_POSITION"),
            avatar_provider=os.environ.get("MEDIA_AGENT_AVATAR_PROVIDER"),
            sadtalker_dir=os.environ.get("MEDIA_AGENT_SADTALKER_DIR"),
            sadtalker_python=os.environ.get("MEDIA_AGENT_SADTALKER_PYTHON"),
            sensitive_level=os.environ.get("MEDIA_AGENT_SENSITIVE_LEVEL"),
            sensitive_words=os.environ.get("MEDIA_AGENT_SENSITIVE_WORDS"),
            promotion_footer=os.environ.get("MEDIA_AGENT_PROMOTION_FOOTER"),
            seo_tags_enabled=os.environ.get(
                "MEDIA_AGENT_SEO_TAGS_ENABLED", "1"
            ) not in ("0", "false", "no", ""),
            wechat_appid=os.environ.get("MEDIA_AGENT_WECHAT_APPID"),
            wechat_appsecret=os.environ.get("MEDIA_AGENT_WECHAT_APPSECRET"),
            wechat_author=os.environ.get("MEDIA_AGENT_WECHAT_AUTHOR"),
            rewrite_style=os.environ.get("MEDIA_AGENT_REWRITE_STYLE"),
            download_images=os.environ.get("MEDIA_AGENT_DOWNLOAD_IMAGES", "1") not in ("0", "false", "no"),
            download_videos=os.environ.get("MEDIA_AGENT_DOWNLOAD_VIDEOS", "1") not in ("0", "false", "no"),
            relevance_filter=os.environ.get("MEDIA_AGENT_RELEVANCE_FILTER", "1") not in ("0", "false", "no"),
            cli_tool=os.environ.get("MEDIA_AGENT_CLI_TOOL", "none"),
            rewrite_priority=os.environ.get("MEDIA_AGENT_REWRITE_PRIORITY", "agent"),
            cli_timeout=_int_env("MEDIA_AGENT_CLI_TIMEOUT") or 500,
            llm_timeout=_int_env("MEDIA_AGENT_LLM_TIMEOUT") or 120,
            fetch_proxy=os.environ.get("MEDIA_AGENT_FETCH_PROXY"),
            github_mirror=os.environ.get("MEDIA_AGENT_GITHUB_MIRROR"),
            huggingface_mirror=os.environ.get("MEDIA_AGENT_HUGGINGFACE_MIRROR"),
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
            "tts_rate": "tts_rate",
            "tts_pitch": "tts_pitch",
            "tts_instruct": "tts_instruct",
            "tts_voice": "tts_voice",
            "video_fit": "video_fit",
            "video_brand_name": "video_brand_name",
            "avatar_image": "avatar_image",
            "avatar_position": "avatar_position",
            "avatar_provider": "avatar_provider",
            "sadtalker_dir": "sadtalker_dir",
            "sadtalker_python": "sadtalker_python",
            "sensitive_level": "sensitive_level",
            "sensitive_words": "sensitive_words",
            "promotion_footer": "promotion_footer",
            "wechat_appid": "wechat_appid",
            "wechat_appsecret": "wechat_appsecret",
            "wechat_author": "wechat_author",
            "rewrite_style": "rewrite_style",
            "cli_tool": "cli_tool",
            "rewrite_priority": "rewrite_priority",
            "fetch_proxy": "fetch_proxy",
            "github_mirror": "github_mirror",
            "huggingface_mirror": "huggingface_mirror",
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

        drafts = store.get_setting("max_drafts")
        if drafts is not None:
            parsed = _int_str(drafts)
            if parsed is not None:
                self.max_drafts = parsed

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

        llm_to = store.get_setting("llm_timeout")
        if llm_to is not None:
            parsed = _int_str(llm_to)
            if parsed is not None:
                self.llm_timeout = parsed

        # Boolean fields
        di = store.get_setting("download_images")
        if di is not None:
            self.download_images = di.strip() not in ("0", "false", "no", "")
        dv = store.get_setting("download_videos")
        if dv is not None:
            self.download_videos = dv.strip() not in ("0", "false", "no", "")
        rf = store.get_setting("relevance_filter")
        if rf is not None:
            self.relevance_filter = rf.strip() not in ("0", "false", "no", "")
        ae = store.get_setting("avatar_enabled")
        if ae is not None:
            self.avatar_enabled = ae.strip() not in ("0", "false", "no", "")
        seo = store.get_setting("seo_tags_enabled")
        if seo is not None:
            self.seo_tags_enabled = seo.strip() not in (
                "0", "false", "no", "")

    def ensure_dirs(self) -> None:
        for d in (self.archive_dir, self.drafts_dir, self.images_dir,
                  self.videos_dir, self.media_dir, self.voices_dir,
                  self.avatar_dir):
            d.mkdir(parents=True, exist_ok=True)
