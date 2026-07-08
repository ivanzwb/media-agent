from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Topic:
    name: str
    keywords: list[str] = field(default_factory=list)


@dataclass
class SourceConfig:
    name: str
    type: str            # "rss" | "scrape"
    url: str
    topics: list[str] = field(default_factory=list)
    mode: str = "single"  # for scrape: "list" | "single"
    include_pattern: str | None = None
    exclude_pattern: str | None = None
    max_pages: int = 3     # for scrape list: follow N pagination pages
    render_js: bool = True  # render with Playwright (SPA/SSR sites) by default
    enabled: bool = True


@dataclass
class FeedsConfig:
    topics: list[Topic]
    sources: list[SourceConfig]

    def add_topic(self, topic: Topic) -> Topic | None:
        if any(t.name == topic.name for t in self.topics):
            return None
        self.topics.append(topic)
        return topic

    def add_source(self, src: SourceConfig) -> SourceConfig | None:
        if any(s.url == src.url for s in self.sources):
            return None
        self.sources.append(src)
        return src


# ── Thread-level lock for feeds.yaml ─────────────────────────────────
# Prevents concurrent read-modify-write races when two uvicorn worker
# threads both load the file before either saves.
_feeds_lock = threading.Lock()


# Empty starter feeds.yaml written on first run of a fresh / packaged install.
# User adds topics & sources via the 「来源」page.
_DEFAULT_FEEDS_YAML = """\
topics: []
sources: []
"""


def ensure_feeds_file(path: Path | str) -> Path:
    """Create a default feeds.yaml at *path* if it doesn't exist. Returns the
    path. Safe to call on every startup (no-op when the file is present)."""
    p = Path(path)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_DEFAULT_FEEDS_YAML, encoding="utf-8")
    return p


def load_feeds(path: Path | str) -> FeedsConfig:
    # A fresh / packaged install has no feeds.yaml yet — don't crash the
    # pipeline; return an empty config (user adds sources via the UI, which
    # writes the file).
    p = Path(path)
    if not p.exists():
        return FeedsConfig(topics=[], sources=[])
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    topics = [Topic(name=t["name"], keywords=t.get("keywords", []))
              for t in data.get("topics", [])]
    sources = []
    for s in data.get("sources", []):
        mode = s.get("mode")
        if not mode:
            mode = "single"
        sources.append(SourceConfig(
            name=s["name"], type=s["type"], url=s["url"],
            topics=s.get("topics", []), mode=mode,
            include_pattern=s.get("include_pattern"),
            exclude_pattern=s.get("exclude_pattern"),
            max_pages=int(s.get("max_pages", 3) or 3),
            render_js=bool(s.get("render_js", True)),
            enabled=s.get("enabled", True),
        ))
    return FeedsConfig(topics=topics, sources=sources)


def _build_feeds_data(config: FeedsConfig) -> dict:
    """Convert FeedsConfig to the dict structure expected by yaml.safe_dump."""
    data = {
        "topics": [{"name": t.name, "keywords": t.keywords}
                   for t in config.topics],
        "sources": [],
    }
    for s in config.sources:
        entry = {"name": s.name, "type": s.type, "url": s.url,
                 "topics": s.topics, "enabled": s.enabled}
        if s.type == "scrape":
            entry["mode"] = s.mode
            if s.include_pattern:
                entry["include_pattern"] = s.include_pattern
            if s.exclude_pattern:
                entry["exclude_pattern"] = s.exclude_pattern
            # Persist only when it differs from the default (max_pages=3,
            # render_js=True) so a non-default value (incl. an explicit
            # render_js: false) round-trips correctly.
            if s.max_pages and s.max_pages != 3:  # noqa: PLR2004
                entry["max_pages"] = s.max_pages
            if not s.render_js:
                entry["render_js"] = s.render_js
        data["sources"].append(entry)
    return data


def _save_feeds_unlocked(config: FeedsConfig, path: Path | str) -> None:
    """Write feeds.yaml via tempfile + rename (caller MUST hold _feeds_lock)."""
    data = _build_feeds_data(config)
    yaml_text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    dst = Path(path)

    # Atomic write: temp file in the same directory → os.replace (atomic on same FS).
    fd, tmp = tempfile.mkstemp(dir=dst.parent, suffix=".yaml", prefix=".feeds_tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(yaml_text)
        os.replace(tmp, str(dst))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_feeds(config: FeedsConfig, path: Path | str) -> None:
    """Atomically write feeds.yaml (thread-safe wrapper)."""
    with _feeds_lock:
        _save_feeds_unlocked(config, path)


def update_feeds(path: Path | str, modifier: Callable[[FeedsConfig], None]) -> None:
    """Load feeds.yaml, run *modifier* (e.g. add_topic/add_source), then save.

    The entire load→modify→save sequence is protected by the thread lock,
    preventing concurrent-reader/writer races.
    """
    with _feeds_lock:
        cfg = load_feeds(path)
        modifier(cfg)
        _save_feeds_unlocked(cfg, path)
