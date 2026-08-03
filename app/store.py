from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import re

import frontmatter
import httpx

from app.config import Config
from app.models import Article, Draft, ArticleStatus, slugify as _slug
from app.pipeline.images import strip_image_prompts

_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
_HTML_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
_VIDEO_PLACEHOLDER_RE = re.compile(r'\[\[(?:VID|VIDEO):\d+\]\]', re.I)


def _has_video(content_md: str, videos: list[str] | None = None) -> bool:
    if videos:
        return True
    content_md = content_md or ""
    if _VIDEO_PLACEHOLDER_RE.search(content_md):
        return True
    # Use the extractor's provider/extension-aware logic so maps, ads and other
    # non-video iframes do not receive a permanent video badge.
    from app.sources.extractor import _videos_from_html
    return bool(_videos_from_html(content_md))


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
    if "../" in src and "/media/" in src:
        # Relative path: ../../media/<hash>/img-N.jpg → data_dir/media/...
        # The draft is at data/drafts/<topic>/<file>.md
        # Going up 2 levels from there reaches data/
        src_path = Path("data") / "media" / src.split("/media/", 1)[1]
        if not src_path.exists():
            src_path = None
    elif src.startswith("/media/"):
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


def _web_image_for_title(title: str, images_dir: Path) -> str | None:
    """Search the web for a relevant cover image when the article has none.
    Uses DuckDuckGo Images (via ddgs).  Returns the filename on success
    or None if no suitable image could be found."""
    try:
        from ddgs import DDGS
        query = f"{title} technology"
        with DDGS() as ddgs:
            results = list(ddgs.images(query, max_results=5))
    except Exception:
        return None
    if not results:
        return None
    # Try each result until one downloads successfully
    for r in results:
        img_url = r.get("image") or r.get("thumbnail")
        if not img_url:
            continue
        try:
            resp = httpx.get(img_url, timeout=15, follow_redirects=True,
                             headers={"User-Agent": "media-agent/0.1"})
            resp.raise_for_status()
            data = resp.content
            if len(data) < 2000:  # skip tiny/error images
                continue
            ext = Path(urlparse(img_url).path).suffix or ".jpg"
            import uuid
            name = f"cover-web-{uuid.uuid4().hex[:8]}{ext}"
            dest = images_dir / name
            images_dir.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return name
        except Exception:
            continue
    return None


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
        topic_dir = str(topic).replace("/", "_").replace("\\", "_").strip(" .")
        topic_dir = topic_dir or "uncategorized"
        date = (article.published_at or article.fetched_at).strftime("%Y%m%d")
        slug = _slug(article.title)[:60]
        if slug == "untitled":
            slug = f"untitled-{article.fingerprint()[:8]}"
        rel = Path("archive") / topic_dir / f"{date}-{slug}.md"
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
             fetched_at, archive_path, has_video, fingerprint, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (article.title, article.url, article.source_name, article.source_type,
             article.topic, _iso(article.published_at), _iso(article.fetched_at),
             article.archive_path,
             1 if _has_video(
                 article.content_md, getattr(article, "videos", []) or [])
             else 0,
             article.fingerprint(), ArticleStatus.ARCHIVED),
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
        # Internal cover-generation prompts are UI metadata, never article
        # content. Strip legacy blocks at the persistence boundary too.
        draft.body_md = strip_image_prompts(draft.body_md)
        topic = draft.topic or "uncategorized"
        topic_dir = str(topic).replace("/", "_").replace("\\", "_").strip(" .")
        topic_dir = topic_dir or "uncategorized"
        slug = _slug(draft.title_candidates[0] if draft.title_candidates
                     else "draft")[:60]
        rel = Path("drafts") / topic_dir / f"{draft.article_id}-{slug}.md"
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
            "origin": getattr(draft, "origin", "rewrite") or "rewrite",
        }
        if getattr(draft, "sources", None):
            meta["sources"] = draft.sources
        if getattr(draft, "citations", None):
            meta["citations"] = draft.citations
        if getattr(draft, "search_meta", None):
            meta["search_meta"] = draft.search_meta
        if getattr(draft, "series_part_label", ""):
            meta["series_part_label"] = draft.series_part_label
        if getattr(draft, "prerequisites", None):
            meta["prerequisites"] = draft.prerequisites
        if draft.title_cn:
            meta["title_cn"] = draft.title_cn
        if getattr(draft, "digest", ""):
            meta["digest"] = draft.digest
        if draft.score is not None:
            meta["score"] = draft.score

        # -- auto-cover: if no cover (or tiny mock placeholder < 10KB),
        #    use the first body image, then fall back to web search --
        if not draft.cover_image or _cover_is_tiny(
            draft.cover_image, self.config.images_dir):
            auto_cover = _first_body_image(
                draft.body_md, self.config.images_dir)
            if not auto_cover:
                # No body images → try web image search
                title = (draft.title_candidates[0]
                         if draft.title_candidates else "")
                auto_cover = _web_image_for_title(
                    title, self.config.images_dir)
            if auto_cover:
                draft.cover_image = auto_cover
            meta["cover_image"] = draft.cover_image

        post = frontmatter.Post(draft.body_md, **meta)
        abs_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        draft.draft_path = str(rel).replace("\\", "/")

        now_iso = datetime.now(timezone.utc).isoformat()
        if getattr(draft, "id", None):
            self.conn.execute(
                """UPDATE drafts SET article_id=?, draft_path=?,
                   cover_image=?, title_cn=?, status=?, updated_at=?, score=?,
                   origin=?, series_id=?, series_order=?
                   WHERE id=?""",
                (draft.article_id, draft.draft_path,
                 draft.cover_image, draft.title_cn, draft.status,
                 now_iso, draft.score, getattr(draft, "origin", "rewrite"),
                 getattr(draft, "series_id", None),
                 getattr(draft, "series_order", None),
                 draft.id))
        else:
            cur = self.conn.execute(
                """INSERT INTO drafts
                (article_id, draft_path, cover_image, title_cn,
                 status, updated_at, score, origin, series_id, series_order)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (draft.article_id, draft.draft_path,
                 draft.cover_image, draft.title_cn, draft.status,
                 now_iso, draft.score, getattr(draft, "origin", "rewrite"),
                 getattr(draft, "series_id", None),
                 getattr(draft, "series_order", None)))
            draft.id = cur.lastrowid
        if draft.id is not None:
            self.conn.execute(
                "DELETE FROM draft_articles WHERE draft_id=?", (draft.id,))
            for index, source in enumerate(
                    getattr(draft, "sources", None) or [], 1):
                article_id = source.get("article_id") if isinstance(source, dict) else None
                if not article_id:
                    continue
                self.conn.execute(
                    """INSERT OR IGNORE INTO draft_articles
                       (draft_id, article_id, rank, role) VALUES (?, ?, ?, ?)""",
                    (draft.id, int(article_id),
                     int(source.get("rank") or index),
                     str(source.get("role") or "source")))
        self.conn.commit()
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

    # ── knowledge series ─────────────────────────────────────────────────
    # Chapter rows are the single source of truth for a series run's progress:
    # the job outlives page refreshes and server restarts, so in-memory state
    # can only ever be a cache of these.

    def create_series(self, *, title: str, topic: str, lang: str, depth: str,
                      parts: int, style_id: str | None,
                      knowledge_map: str = "", ref_count: int = 5,
                      engines: list[str] | None = None,
                      status: str = "running") -> int:
        cur = self.conn.execute(
            """INSERT INTO series
               (title, topic, lang, depth, parts, style_id, ref_count, engines,
                knowledge_map, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (title, topic, lang, depth, int(parts), style_id, int(ref_count),
             json.dumps(list(engines or []), ensure_ascii=False),
             knowledge_map, status,
             datetime.now(timezone.utc).isoformat()))
        self.conn.commit()
        return int(cur.lastrowid)

    def mark_series_running(self, series_id: int) -> None:
        self.conn.execute(
            "UPDATE series SET status='running', error=NULL, finished_at=NULL"
            " WHERE id=?", (series_id,))
        self.conn.commit()

    def finish_series(self, series_id: int, status: str,
                      error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE series SET status=?, error=?, finished_at=? WHERE id=?",
            (status, error, datetime.now(timezone.utc).isoformat(), series_id))
        self.conn.commit()

    def get_series(self, series_id: int):
        return self.conn.execute(
            "SELECT * FROM series WHERE id=?", (series_id,)).fetchone()

    def list_series(self, limit: int | None = None):
        sql = "SELECT * FROM series ORDER BY id DESC"
        params: list = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return self.conn.execute(sql, params).fetchall()

    def latest_series(self):
        return self.conn.execute(
            "SELECT * FROM series ORDER BY id DESC LIMIT 1").fetchone()

    def add_chapters(self, series_id: int, chapters: list[dict]) -> list[int]:
        now_iso = datetime.now(timezone.utc).isoformat()
        ids: list[int] = []
        for index, chapter in enumerate(chapters, 1):
            cur = self.conn.execute(
                """INSERT INTO series_chapters
                   (series_id, chapter_order, title, scope, search_queries,
                    prerequisites, preflight_hits, status, updated_at)
                   VALUES (?,?,?,?,?,?,?,'pending',?)""",
                (series_id, index,
                 str(chapter.get("title") or f"第 {index} 章"),
                 str(chapter.get("scope") or ""),
                 json.dumps(chapter.get("search_queries") or [],
                            ensure_ascii=False),
                 json.dumps(chapter.get("prerequisites") or [],
                            ensure_ascii=False),
                 chapter.get("preflight_hits"),
                 now_iso))
            ids.append(int(cur.lastrowid))
        self.conn.commit()
        return ids

    def replace_chapters(self, series_id: int,
                         chapters: list[dict]) -> list[int]:
        """Swap in a reviewed outline, before any chapter has been written.

        Editing an outline is reordering as much as rewording, and chapter
        order is a column, so the rows are rebuilt rather than patched.
        """
        written = [row for row in self.list_chapters(series_id)
                   if row["draft_id"]]
        if written:
            raise ValueError("已经写出草稿的系列不能再改提纲")
        self.conn.execute(
            "DELETE FROM series_chapters WHERE series_id=?", (series_id,))
        self.conn.execute(
            "UPDATE series SET parts=? WHERE id=?", (len(chapters), series_id))
        return self.add_chapters(series_id, chapters)

    def list_chapters(self, series_id: int):
        return self.conn.execute(
            "SELECT * FROM series_chapters WHERE series_id=? "
            "ORDER BY chapter_order", (series_id,)).fetchall()

    def update_chapter(self, chapter_id: int, **fields) -> None:
        allowed = [key for key in ("status", "error", "draft_id", "summary")
                   if key in fields]
        if not allowed:
            return
        params = [fields[key] for key in allowed]
        params.append(datetime.now(timezone.utc).isoformat())
        params.append(chapter_id)
        self.conn.execute(
            f"UPDATE series_chapters SET {', '.join(f'{k}=?' for k in allowed)},"
            " updated_at=? WHERE id=?", params)
        self.conn.commit()

    def refresh_series_status(self, series_id: int) -> str:
        """Recompute a finished series' status from its chapter rows.

        Reruns change the picture after the run is over — a failed chapter can
        become the difference between "partial" and "done".
        """
        statuses = [row["status"] for row in self.list_chapters(series_id)]
        if not statuses:
            return "failed"
        if any(item in ("researching", "writing") for item in statuses):
            return "running"
        if not any(item == "done" for item in statuses):
            status = "failed"
        elif all(item == "done" for item in statuses):
            status = "done"
        else:
            status = "partial"
        self.finish_series(series_id, status)
        return status

    def series_source_urls(self, series_id: int,
                           exclude_draft_id: int | None = None) -> set[str]:
        """URLs the series' other chapters already cited."""
        sql = ("SELECT articles.url FROM draft_articles "
               "JOIN drafts ON drafts.id = draft_articles.draft_id "
               "JOIN articles ON articles.id = draft_articles.article_id "
               "WHERE drafts.series_id=?")
        params: list = [series_id]
        if exclude_draft_id is not None:
            sql += " AND drafts.id<>?"
            params.append(int(exclude_draft_id))
        return {row[0] for row in self.conn.execute(sql, params).fetchall()}

    def get_series_drafts(self, series_id: int):
        return self.conn.execute(
            "SELECT drafts.*, articles.title AS article_title "
            "FROM drafts LEFT JOIN articles ON articles.id = drafts.article_id "
            "WHERE drafts.series_id=? ORDER BY drafts.series_order",
            (series_id,)).fetchall()

    def article_video_flags(self, rows) -> dict[int, bool]:
        """Return video flags, lazily backfilling legacy rows in one commit."""
        flags: dict[int, bool] = {}
        updates: list[tuple[int, int]] = []
        for row in rows:
            cached = row["has_video"] if "has_video" in row.keys() else None
            if cached is None:
                meta = self.read_article_body(row["id"])
                has_video = _has_video(
                    meta.get("content_md", ""), meta.get("videos") or [])
                updates.append((1 if has_video else 0, row["id"]))
            else:
                has_video = bool(cached)
            flags[row["id"]] = has_video
        if updates:
            self.conn.executemany(
                "UPDATE articles SET has_video=? WHERE id=?", updates)
            self.conn.commit()
        return flags

    # ----- read/update helpers for the web UI -----

    def list_sources(self):
        rows = self.conn.execute(
            "SELECT DISTINCT source_name FROM articles WHERE source_name IS NOT NULL "
            "ORDER BY source_name").fetchall()
        return [r["source_name"] for r in rows]

    @staticmethod
    def _article_filter(topic: str | None,
                        source: str | None) -> tuple[str, list]:
        clauses = []
        params: list = []
        if topic:
            clauses.append("topic=?")
            params.append(topic)
        if source:
            clauses.append("source_name=?")
            params.append(source)
        return ("WHERE " + " AND ".join(clauses)) if clauses else "", params

    def list_articles(self, limit: int = 100, topic: str | None = None,
                      source: str | None = None, offset: int = 0):
        where, params = self._article_filter(topic, source)
        return self.conn.execute(
            f"SELECT * FROM articles {where} "
            "ORDER BY COALESCE(published_at, fetched_at) DESC "
            "LIMIT ? OFFSET ?",
            (*params, limit, offset)).fetchall()

    def count_articles(self, topic: str | None = None,
                       source: str | None = None) -> int:
        where, params = self._article_filter(topic, source)
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM articles {where}", params).fetchone()
        return int(row["n"]) if row else 0

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
        self.conn.execute(
            "UPDATE articles SET has_video=? WHERE id=?",
            (1 if _has_video(content_md, videos) else 0, article_id),
        )
        self.conn.commit()

    def drafts_by_article(self, article_ids=None) -> dict[int, int]:
        """Map article_id -> a draft id.

        Scoped to the articles being shown when ids are given: the archive
        page needs this for one screenful, not for every draft ever written.
        """
        if article_ids is not None:
            ids = [int(value) for value in article_ids]
            if not ids:
                return {}
            marks = ",".join("?" * len(ids))
            rows = self.conn.execute(
                f"SELECT id, article_id FROM drafts WHERE article_id IN ({marks}) "
                "ORDER BY id", ids).fetchall()
        else:
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

    def delete_drafts(self, ids: list[int]) -> int:
        """Delete specific drafts (DB rows + their markdown files). Returns the
        number of drafts deleted."""
        if not ids:
            return 0
        deleted = 0
        for did in ids:
            row = self.get_draft(did)
            if not row:
                continue
            self.conn.execute("DELETE FROM drafts WHERE id=?", (did,))
            if row["draft_path"]:
                dp = self.config.data_dir / row["draft_path"]
                try:
                    if dp.exists():
                        dp.unlink()
                except OSError:
                    pass
            deleted += 1
        self.conn.commit()
        return deleted

    def delete_articles(self, ids: list[int]) -> int:
        """Delete specific articles (and their dependent drafts) from the DB and
        remove their archive/draft files. Returns the number of articles deleted."""
        if not ids:
            return 0
        deleted = 0
        for aid in ids:
            art = self.get_article(aid)
            if not art:
                continue
            # Dependent drafts first (FK): remove rows + files.
            drows = self.conn.execute(
                "SELECT id, draft_path FROM drafts WHERE article_id=?",
                (aid,)).fetchall()
            for d in drows:
                self.conn.execute("DELETE FROM drafts WHERE id=?", (d["id"],))
                if d["draft_path"]:
                    dp = self.config.data_dir / d["draft_path"]
                    try:
                        if dp.exists():
                            dp.unlink()
                    except OSError:
                        pass
            self.conn.execute("DELETE FROM articles WHERE id=?", (aid,))
            if art["archive_path"]:
                ap = self.config.data_dir / art["archive_path"]
                try:
                    if ap.exists():
                        ap.unlink()
                except OSError:
                    pass
            deleted += 1
        self.conn.commit()
        return deleted

    def list_topics(self):
        rows = self.conn.execute(
            "SELECT DISTINCT topic FROM articles WHERE topic IS NOT NULL "
            "ORDER BY topic").fetchall()
        return [r["topic"] for r in rows]

    def list_drafts(self, status: str | None = None,
                    limit: int | None = None, offset: int = 0):
        sql = (
            "SELECT drafts.*, articles.published_at AS article_published_at, "
            "articles.title AS article_title, articles.url AS article_url "
            "FROM drafts "
            "LEFT JOIN articles ON articles.id = drafts.article_id"
        )
        params: list = []
        if status:
            sql += " WHERE drafts.status=?"
            params.append(status)
        sql += " ORDER BY drafts.updated_at DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([int(limit), int(offset)])
        return self.conn.execute(sql, params).fetchall()

    def count_drafts(self, status: str | None = None) -> int:
        if status:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM drafts WHERE status=?", (status,)).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) FROM drafts").fetchone()
        return int(row[0]) if row else 0

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
        meta["body_md"] = strip_image_prompts(post.content)
        return meta

    def update_draft_body(self, draft_id: int, title_candidates: list[str],
                          body_md: str, status: str | None = None,
                          title_cn: str | None = None,
                          digest: str | None = None) -> None:
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
        if digest is not None:
            if digest:
                meta["digest"] = digest
            else:
                meta.pop("digest", None)
        new_status = status or meta.get("status") or row["status"]
        meta["status"] = new_status
        post = frontmatter.Post(strip_image_prompts(body_md), **meta)
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
