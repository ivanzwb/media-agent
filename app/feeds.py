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
            enabled=s.get("enabled", True),
        ))
    return FeedsConfig(topics=topics, sources=sources)
