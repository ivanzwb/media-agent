from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse

_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                    "utm_content", "utm", "ref", "ref_src", "spm"}


class ArticleStatus:
    ARCHIVED = "archived"
    CLASSIFIED = "classified"
    DRAFTED = "drafted"


class DraftStatus:
    DRAFTED = "drafted"
    REVIEWING = "reviewing"
    APPROVED = "approved"
    PUBLISHED = "published"


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return text or "untitled"


@dataclass
class Article:
    title: str
    content_md: str
    url: str
    source_name: str
    source_type: str
    published_at: datetime | None
    images: list[str]
    raw_summary: str | None
    fetched_at: datetime
    topic: str | None = None
    archive_path: str | None = None
    id: int | None = None
    videos: list[str] = field(default_factory=list)

    def normalized_url(self) -> str:
        parts = urlparse(self.url)
        query = [(k, v) for k, v in parse_qsl(parts.query)
                 if k.lower() not in _TRACKING_PARAMS]
        path = parts.path.rstrip("/") or "/"
        return urlunparse(parts._replace(path=path, query=urlencode(query),
                                         fragment=""))

    def fingerprint(self) -> str:
        return hashlib.sha256(self.normalized_url().encode("utf-8")).hexdigest()


@dataclass
class Draft:
    article_id: int
    title_candidates: list[str]
    body_md: str
    topic: str
    source_url: str
    source_name: str
    cover_image: str | None = None
    title_cn: str | None = None
    flagged_claims: list[str] = field(default_factory=list)
    status: str = DraftStatus.DRAFTED
    draft_path: str | None = None
    id: int | None = None
    sensitive_hits: list[str] = field(default_factory=list)
    score: float | None = None
    origin: str = "rewrite"
    sources: list[dict] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)
    search_meta: dict = field(default_factory=dict)
