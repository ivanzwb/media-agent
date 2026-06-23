from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from app.config import Config
from app.models import Article, Draft, ArticleStatus, slugify as _slug


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


class Store:
    def __init__(self, conn: sqlite3.Connection, config: Config):
        self.conn = conn
        self.config = config

    def exists(self, fingerprint: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM articles WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        return row is not None

    def _get_by_fingerprint(self, fingerprint: str):
        return self.conn.execute(
            "SELECT * FROM articles WHERE fingerprint=?", (fingerprint,)
        ).fetchone()

    def save_article(self, article: Article) -> Article:
        existing = self._get_by_fingerprint(article.fingerprint())
        if existing:
            article.id = existing["id"]
            article.archive_path = existing["archive_path"]
            return article

        topic = article.topic or "uncategorized"
        date = (article.published_at or article.fetched_at).strftime("%Y%m%d")
        slug = _slug(article.title)[:60]
        rel = Path("archive") / topic / f"{date}-{slug}.md"
        abs_path = self.config.data_dir / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        post = frontmatter.Post(article.content_md, **{
            "title": article.title,
            "url": article.url,
            "source_name": article.source_name,
            "source_type": article.source_type,
            "topic": topic,
            "published_at": _iso(article.published_at),
            "images": article.images,
        })
        abs_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        article.archive_path = str(rel).replace("\\", "/")

        cur = self.conn.execute(
            """INSERT INTO articles
            (title, url, source_name, source_type, topic, published_at,
             fetched_at, archive_path, fingerprint, status)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (article.title, article.url, article.source_name, article.source_type,
             article.topic, _iso(article.published_at), _iso(article.fetched_at),
             article.archive_path, article.fingerprint(), ArticleStatus.ARCHIVED),
        )
        self.conn.commit()
        article.id = cur.lastrowid
        return article

    def set_topic(self, article_id: int, topic: str) -> None:
        self.conn.execute(
            "UPDATE articles SET topic=?, status=? WHERE id=?",
            (topic, ArticleStatus.CLASSIFIED, article_id))
        self.conn.commit()

    def save_draft(self, draft: Draft) -> Draft:
        topic = draft.topic or "uncategorized"
        slug = _slug(draft.title_candidates[0] if draft.title_candidates
                     else "draft")[:60]
        rel = Path("drafts") / topic / f"{draft.article_id}-{draft.platform}-{slug}.md"
        abs_path = self.config.data_dir / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        post = frontmatter.Post(draft.body_md, **{
            "title_candidates": draft.title_candidates,
            "topic": draft.topic,
            "source_url": draft.source_url,
            "source_name": draft.source_name,
            "platform": draft.platform,
            "cover_image": draft.cover_image,
            "flagged_claims": draft.flagged_claims,
            "status": draft.status,
        })
        abs_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        draft.draft_path = str(rel).replace("\\", "/")

        cur = self.conn.execute(
            """INSERT INTO drafts
            (article_id, platform, draft_path, cover_image, status, updated_at)
            VALUES (?,?,?,?,?,?)""",
            (draft.article_id, draft.platform, draft.draft_path,
             draft.cover_image, draft.status,
             datetime.now(timezone.utc).isoformat()))
        self.conn.commit()
        draft.id = cur.lastrowid
        return draft
