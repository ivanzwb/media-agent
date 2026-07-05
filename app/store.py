from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import re

import frontmatter
import httpx

from app.config import Config
from app.models import Article, Draft, ArticleStatus, slugify as _slug

_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
_HTML_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)


def _cover_is_tiny(cover_name: str, images_dir: Path) -> bool:
    """Return True if the cover file exists but is too small (< 10KB)
    — likely a mock-generated placeholder that WeChat rejects."""
    p = (images_dir / cover_name).resolve()
    return p.exists() and p.stat().st_size < 10_000


def _first_body_image(body_md: str, images_dir: Path) -> str | None:
    """Extract the first image URL from markdown/HTML body and copy it to
    images_dir as a cover file.  Returns the filename (e.g. cover-xxx.jpg)
    or None."""
    # Find first image in body
    urls = _MD_IMG_RE.findall(body_md) + _HTML_IMG_RE.findall(body_md)
    src = urls[0] if urls else None
    if not src:
        return None

    # Resolve to a local file
    src_path: Path | None = None
    is_temp = False
    if src.startswith("/media/"):
        # /media/<hash>/img-N.jpg → data_dir/media/<hash>/img-N.jpg
        src_path = Path("data") / "media" / src[len("/media/"):].lstrip("/")
        if not src_path.exists():
            src_path = None
    elif src.startswith("/images/"):
        src_path = images_dir / src[len("/images/"):]
        if not src_path.exists():
            src_path = None
    elif src.startswith(("/videos/", "data:")):
        return None
    elif src.startswith(("http://", "https://")):
        # Download remote image
        try:
            resp = httpx.get(src, timeout=20, follow_redirects=True)
            resp.raise_for_status()
            ext = Path(urlparse(src).path).suffix or ".jpg"
            import tempfile, os
            fd, tmp = tempfile.mkstemp(suffix=ext)
            Path(tmp).write_bytes(resp.content)
            os.close(fd)
            src_path = Path(tmp)
            is_temp = True
        except Exception:
            return None
    else:
        return None

    if src_path is None or not src_path.exists():
        return None

    # Generate a unique cover name
    ext = src_path.suffix or ".jpg"
    import uuid
    name = f"cover-auto-{uuid.uuid4().hex[:8]}{ext}"
    dest = images_dir / name
    images_dir.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(str(src_path), str(dest))
    if is_temp:
        try:
            src_path.unlink()
        except OSError:
            pass
    return name


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

    def exists_by_title_source(self, title: str, source_name: str) -> bool:
        """Check if an article with the same title from the same source
        already exists — catches duplicates that differ by URL but not content."""
        row = self.conn.execute(
            "SELECT 1 FROM articles WHERE title=? AND source_name=?",
            (title, source_name),
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
            "videos": getattr(article, "videos", []) or [],
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
        rel = Path("drafts") / topic / f"{draft.article_id}-{slug}.md"
        abs_path = self.config.data_dir / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        meta: dict[str, object] = {
            "title_candidates": draft.title_candidates,
            "topic": draft.topic,
            "source_url": draft.source_url,
            "source_name": draft.source_name,
            "cover_image": draft.cover_image,
            "flagged_claims": draft.flagged_claims,
            "sensitive_hits": getattr(draft, "sensitive_hits", []) or [],
            "status": draft.status,
        }
        if draft.title_cn:
            meta["title_cn"] = draft.title_cn
        if draft.score is not None:
            meta["score"] = draft.score

        # -- auto-cover: if no cover (or tiny mock placeholder < 10KB),
        #    use the first body image --
        if not draft.cover_image or _cover_is_tiny(
            draft.cover_image, self.config.images_dir):
            auto_cover = _first_body_image(
                draft.body_md, self.config.images_dir)
            if auto_cover:
                draft.cover_image = auto_cover
            meta["cover_image"] = draft.cover_image

        post = frontmatter.Post(draft.body_md, **meta)
        abs_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        draft.draft_path = str(rel).replace("\\", "/")

        cur = self.conn.execute(
            """INSERT INTO drafts
            (article_id, draft_path, cover_image, title_cn,
             status, updated_at, score)
            VALUES (?,?,?,?,?,?,?)""",
            (draft.article_id, draft.draft_path,
             draft.cover_image, draft.title_cn, draft.status,
             datetime.now(timezone.utc).isoformat(),
             draft.score))
        self.conn.commit()
        draft.id = cur.lastrowid
        return draft

    def update_draft_score(self, draft_id: int, score: float) -> None:
        """Update the score for an existing draft."""
        self.conn.execute(
            "UPDATE drafts SET score=?, updated_at=? WHERE id=?",
            (score, datetime.now(timezone.utc).isoformat(), draft_id))
        self.conn.commit()
        # Also update the frontmatter in the markdown file
        row = self.get_draft(draft_id)
        if row and row["draft_path"]:
            abs_path = self.config.data_dir / row["draft_path"]
            if abs_path.exists():
                post = frontmatter.load(str(abs_path))
                post["score"] = score
                abs_path.write_text(frontmatter.dumps(post), encoding="utf-8")

    # ----- read/update helpers for the web UI -----

    def list_sources(self):
        rows = self.conn.execute(
            "SELECT DISTINCT source_name FROM articles WHERE source_name IS NOT NULL "
            "ORDER BY source_name").fetchall()
        return [r["source_name"] for r in rows]

    def list_articles(self, limit: int = 100, topic: str | None = None,
                      source: str | None = None):
        clauses = []
        params = []
        if topic:
            clauses.append("topic=?")
            params.append(topic)
        if source:
            clauses.append("source_name=?")
            params.append(source)
        where = ""
        if clauses:
            where = "WHERE " + " AND ".join(clauses)
        return self.conn.execute(
            f"SELECT * FROM articles {where} "
            "ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT ?",
            (*params, limit)).fetchall()

    def get_article(self, article_id: int):
        return self.conn.execute(
            "SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()

    def read_article_body(self, article_id: int) -> dict:
        """Read the archived markdown (front-matter + content) for an article."""
        row = self.get_article(article_id)
        if not row or not row["archive_path"]:
            return {}
        abs_path = self.config.data_dir / row["archive_path"]
        if not abs_path.exists():
            return {}
        post = frontmatter.load(str(abs_path))
        meta = dict(post.metadata)
        meta["content_md"] = post.content
        return meta

    def update_article_archive(self, article_id: int, content_md: str,
                               images: list[str], videos: list[str]) -> None:
        """Rewrite an archive's body + front-matter images/videos (after
        localizing media to local paths)."""
        row = self.get_article(article_id)
        if not row or not row["archive_path"]:
            return
        abs_path = self.config.data_dir / row["archive_path"]
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        if abs_path.exists():
            post = frontmatter.load(str(abs_path))
            post["images"] = images
            post["videos"] = videos
            post.content = content_md
        else:
            # File missing (e.g. after refetch of a refetched article) —
            # recreate it from DB row metadata + new content.
            post = frontmatter.Post(content_md, **{
                "title": row["title"],
                "url": row["url"],
                "source_name": row["source_name"],
                "source_type": row["source_type"],
                "topic": row["topic"],
                "published_at": row["published_at"],
                "images": images,
                "videos": videos,
            })
        abs_path.write_text(frontmatter.dumps(post), encoding="utf-8")

    def drafts_by_article(self) -> dict[int, int]:
        """Map article_id -> a draft id."""
        rows = self.conn.execute(
            "SELECT id, article_id FROM drafts ORDER BY id").fetchall()
        out: dict[int, int] = {}
        for r in rows:
            aid = r["article_id"]
            if aid not in out:
                out[aid] = r["id"]
        return out

    def get_draft_for_article(self, article_id: int):
        return self.conn.execute(
            "SELECT * FROM drafts WHERE article_id=? "
            "ORDER BY id DESC LIMIT 1", (article_id,)).fetchone()

    def delete_draft(self, draft_id: int) -> None:
        row = self.get_draft(draft_id)
        if not row:
            return
        # Commit DB delete FIRST, then clean up the file.
        # This prevents orphaned DB records (file gone but row remains)
        # when the commit fails after the file is already deleted.
        self.conn.execute("DELETE FROM drafts WHERE id=?", (draft_id,))
        self.conn.commit()
        # Best-effort file cleanup after DB record is safely removed.
        if row["draft_path"]:
            abs_path = self.config.data_dir / row["draft_path"]
            try:
                if abs_path.exists():
                    abs_path.unlink()
            except OSError:
                pass

    def list_topics(self):
        rows = self.conn.execute(
            "SELECT DISTINCT topic FROM articles WHERE topic IS NOT NULL "
            "ORDER BY topic").fetchall()
        return [r["topic"] for r in rows]

    def list_drafts(self, status: str | None = None):
        sql = (
            "SELECT drafts.*, articles.published_at AS article_published_at, "
            "articles.title AS article_title, articles.url AS article_url "
            "FROM drafts "
            "LEFT JOIN articles ON articles.id = drafts.article_id"
        )
        if status:
            sql += " WHERE drafts.status=?"
            sql += " ORDER BY drafts.updated_at DESC"
            return self.conn.execute(sql, (status,)).fetchall()
        sql += " ORDER BY drafts.updated_at DESC"
        return self.conn.execute(sql).fetchall()

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
                          body_md: str, status: str | None = None,
                          title_cn: str | None = None) -> None:
        row = self.get_draft(draft_id)
        if not row or not row["draft_path"]:
            return
        abs_path = self.config.data_dir / row["draft_path"]
        existing = frontmatter.load(str(abs_path)) if abs_path.exists() else \
            frontmatter.Post("")
        meta = dict(existing.metadata)
        meta["title_candidates"] = title_candidates
        if title_cn is not None:
            if title_cn:
                meta["title_cn"] = title_cn
            else:
                meta.pop("title_cn", None)
        new_status = status or meta.get("status") or row["status"]
        meta["status"] = new_status
        post = frontmatter.Post(body_md, **meta)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        if title_cn is not None:
            self.conn.execute(
                "UPDATE drafts SET status=?, title_cn=?, updated_at=? WHERE id=?",
                (new_status, title_cn or None,
                 datetime.now(timezone.utc).isoformat(), draft_id))
        else:
            self.conn.execute(
                "UPDATE drafts SET status=?, updated_at=? WHERE id=?",
                (new_status,
                 datetime.now(timezone.utc).isoformat(), draft_id))
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

    # ----- clear helpers -----

    def clear_articles(self) -> int:
        """Delete all articles (and dependent drafts) from DB and remove files."""
        import shutil
        # Drafts depend on articles via FK — delete them first
        draft_count = self.conn.execute("SELECT COUNT(*) AS c FROM drafts").fetchone()["c"]
        self.conn.execute("DELETE FROM drafts")
        count = self.conn.execute("SELECT COUNT(*) AS c FROM articles").fetchone()["c"]
        self.conn.execute("DELETE FROM articles")
        self.conn.commit()
        # Remove file trees
        for sub in ("archive", "drafts"):
            d = self.config.data_dir / sub
            if d.exists():
                shutil.rmtree(str(d))
                d.mkdir(parents=True, exist_ok=True)
        return count + draft_count

    def clear_drafts(self) -> int:
        """Delete all drafts from DB and remove draft files."""
        import shutil
        count = self.conn.execute("SELECT COUNT(*) AS c FROM drafts").fetchone()["c"]
        self.conn.execute("DELETE FROM drafts")
        self.conn.commit()
        drafts_dir = self.config.data_dir / "drafts"
        if drafts_dir.exists():
            shutil.rmtree(str(drafts_dir))
            drafts_dir.mkdir(parents=True, exist_ok=True)
        return count

    def clear_runs(self) -> int:
        """Delete all run history records."""
        count = self.conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        self.conn.execute("DELETE FROM runs")
        self.conn.commit()
        return count
