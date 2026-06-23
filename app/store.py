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

    # ----- read/update helpers for the web UI -----

    def list_articles(self, limit: int = 100, topic: str | None = None):
        if topic:
            return self.conn.execute(
                """SELECT * FROM articles WHERE topic=?
                   ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT ?""",
                (topic, limit)).fetchall()
        return self.conn.execute(
            """SELECT * FROM articles
               ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT ?""",
            (limit,)).fetchall()

    def get_article(self, article_id: int):
        return self.conn.execute(
            "SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()

    def list_topics(self):
        rows = self.conn.execute(
            "SELECT DISTINCT topic FROM articles WHERE topic IS NOT NULL "
            "ORDER BY topic").fetchall()
        return [r["topic"] for r in rows]

    def list_drafts(self, status: str | None = None):
        if status:
            return self.conn.execute(
                "SELECT * FROM drafts WHERE status=? ORDER BY updated_at DESC",
                (status,)).fetchall()
        return self.conn.execute(
            "SELECT * FROM drafts ORDER BY updated_at DESC").fetchall()

    def get_draft(self, draft_id: int):
        return self.conn.execute(
            "SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()

    def read_draft_body(self, draft_id: int) -> dict:
        row = self.get_draft(draft_id)
        if not row or not row["draft_path"]:
            return {}
        abs_path = self.config.data_dir / row["draft_path"]
        if not abs_path.exists():
            return {}
        post = frontmatter.load(str(abs_path))
        meta = dict(post.metadata)
        meta["body_md"] = post.content
        return meta

    def update_draft_body(self, draft_id: int, title_candidates: list[str],
                          body_md: str, status: str | None = None) -> None:
        row = self.get_draft(draft_id)
        if not row or not row["draft_path"]:
            return
        abs_path = self.config.data_dir / row["draft_path"]
        existing = frontmatter.load(str(abs_path)) if abs_path.exists() else \
            frontmatter.Post("")
        meta = dict(existing.metadata)
        meta["title_candidates"] = title_candidates
        new_status = status or meta.get("status") or row["status"]
        meta["status"] = new_status
        post = frontmatter.Post(body_md, **meta)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        self.conn.execute(
            "UPDATE drafts SET status=?, updated_at=? WHERE id=?",
            (new_status, datetime.now(timezone.utc).isoformat(), draft_id))
        self.conn.commit()

    def set_draft_status(self, draft_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE drafts SET status=?, updated_at=? WHERE id=?",
            (status, datetime.now(timezone.utc).isoformat(), draft_id))
        self.conn.commit()

    def set_draft_cover(self, draft_id: int, cover_image: str | None) -> None:
        self.conn.execute(
            "UPDATE drafts SET cover_image=?, updated_at=? WHERE id=?",
            (cover_image, datetime.now(timezone.utc).isoformat(), draft_id))
        self.conn.commit()
        row = self.get_draft(draft_id)
        if row and row["draft_path"]:
            abs_path = self.config.data_dir / row["draft_path"]
            if abs_path.exists():
                post = frontmatter.load(str(abs_path))
                post["cover_image"] = cover_image
                abs_path.write_text(frontmatter.dumps(post), encoding="utf-8")

    def record_run(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, status) VALUES (?, 'running')",
            (datetime.now(timezone.utc).isoformat(),))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, stats_json: str,
                   status: str = "done") -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, stats_json=?, status=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), stats_json, status, run_id))
        self.conn.commit()

    def list_runs(self, limit: int = 20):
        return self.conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?",
            (limit,)).fetchall()

    def dashboard_stats(self) -> dict:
        articles = self.conn.execute(
            "SELECT COUNT(*) AS c FROM articles").fetchone()["c"]
        drafts = self.conn.execute(
            "SELECT COUNT(*) AS c FROM drafts").fetchone()["c"]
        runs = self.conn.execute(
            "SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        return {"articles": articles, "drafts": drafts, "runs": runs}

    def get_setting(self, key: str, default=None):
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))
        self.conn.commit()

    def delete_setting(self, key: str) -> None:
        self.conn.execute("DELETE FROM settings WHERE key=?", (key,))
        self.conn.commit()
