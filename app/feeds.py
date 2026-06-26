from __future__ import annotations

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
    max_pages: int = 1     # for scrape list: follow N pagination pages
    render_js: bool = False  # render with Playwright (SPA/SSR sites)
    enabled: bool = True


@dataclass
class FeedsConfig:
    topics: list[Topic]
    sources: list[SourceConfig]


def load_feeds(path: Path | str) -> FeedsConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
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
            max_pages=int(s.get("max_pages", 1) or 1),
            render_js=bool(s.get("render_js", False)),
            enabled=s.get("enabled", True),
        ))
    return FeedsConfig(topics=topics, sources=sources)


def save_feeds(config: FeedsConfig, path: Path | str) -> None:
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
            if s.max_pages and s.max_pages > 1:
                entry["max_pages"] = s.max_pages
            if s.render_js:
                entry["render_js"] = s.render_js
        data["sources"].append(entry)
    Path(path).write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
