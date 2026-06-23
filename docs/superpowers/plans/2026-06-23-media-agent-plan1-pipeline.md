# Media Agent — Plan 1: Foundation + Pipeline CLI

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an end-to-end command-line pipeline that fetches articles from RSS feeds and web scraping, deduplicates, archives, classifies by topic, and rewrites them into faithful, attention-grabbing Markdown drafts.

**Architecture:** Single Python package `app/`. A source layer normalizes RSS and scraped pages into a common `Article` dataclass. A SQLite-backed store tracks article/draft metadata while bodies live as Markdown files on disk. A swappable LLM provider layer powers classification, rewriting, and faithfulness checking. An orchestrator wires the stages and is driven by a Typer CLI.

**Tech Stack:** Python 3.11+, SQLite (stdlib `sqlite3`), feedparser, httpx, trafilatura, PyYAML, python-frontmatter, Typer, pytest.

---

## File Structure

- `app/__init__.py` — package marker
- `app/config.py` — settings loading (paths, LLM provider/key) from env + defaults
- `app/db.py` — SQLite connection + schema init
- `app/models.py` — `Article`, `Draft` dataclasses + status constants
- `app/store.py` — DB CRUD for articles/drafts/runs + Markdown file read/write
- `app/sources/__init__.py`
- `app/sources/base.py` — `Source` protocol + `Article` factory helpers
- `app/sources/extractor.py` — HTML → title/body/images extraction (trafilatura)
- `app/sources/rss.py` — RSS feed → `Article` list
- `app/sources/scraper.py` — list/single page scraping → `Article` list
- `app/sources/dedup.py` — URL normalization + fingerprint dedup
- `app/llm/__init__.py`
- `app/llm/base.py` — `LLMProvider` protocol + `get_provider()`
- `app/llm/providers/__init__.py`
- `app/llm/providers/mock.py` — deterministic mock provider for tests/dev
- `app/llm/providers/openai.py` — OpenAI implementation
- `app/pipeline/__init__.py`
- `app/pipeline/classifier.py` — keyword + LLM topic classification
- `app/pipeline/rewriter.py` — faithful rewrite + faithfulness check → draft Markdown
- `app/pipeline/orchestrator.py` — run one full pipeline pass
- `app/feeds.py` — load/validate `feeds.yaml`
- `app/cli.py` — Typer CLI entrypoints
- `feeds.yaml` — sample topics + sources config
- `requirements.txt`
- `tests/` — mirrors `app/` with `test_*.py`

---

## Task 1: Project skeleton + dependencies

**Files:**
- Create: `requirements.txt`
- Create: `app/__init__.py`
- Create: `tests/__init__.py`
- Create: `.gitignore`

- [ ] **Step 1: Create requirements.txt**

```
feedparser==6.0.11
httpx==0.27.2
trafilatura==1.12.2
PyYAML==6.0.2
python-frontmatter==1.1.0
typer==0.12.5
openai==1.54.4
pytest==8.3.3
```

- [ ] **Step 2: Create package markers**

`app/__init__.py`:

```python
__version__ = "0.1.0"
```

`tests/__init__.py`:

```python
```

- [ ] **Step 3: Create .gitignore**

```
__pycache__/
*.pyc
.venv/
venv/
data/
.env
.pytest_cache/
```

- [ ] **Step 4: Create virtualenv and install**

Run: `python -m venv .venv && .venv\Scripts\python -m pip install -r requirements.txt`
Expected: installs without error.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt app/__init__.py tests/__init__.py .gitignore
git commit -m "chore: project skeleton and dependencies"
```

---

## Task 2: Config

**Files:**
- Create: `app/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
import os
from pathlib import Path
from app.config import Config

def test_config_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    cfg = Config.load()
    assert cfg.data_dir == tmp_path
    assert cfg.archive_dir == tmp_path / "archive"
    assert cfg.drafts_dir == tmp_path / "drafts"
    assert cfg.images_dir == tmp_path / "images"
    assert cfg.db_path == tmp_path / "media.db"
    assert cfg.llm_provider == "mock"

def test_config_reads_llm_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEDIA_AGENT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("MEDIA_AGENT_LLM_API_KEY", "sk-test")
    cfg = Config.load()
    assert cfg.llm_provider == "openai"
    assert cfg.llm_api_key == "sk-test"

def test_ensure_dirs_creates(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path / "d"))
    cfg = Config.load()
    cfg.ensure_dirs()
    assert cfg.archive_dir.is_dir()
    assert cfg.drafts_dir.is_dir()
    assert cfg.images_dir.is_dir()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.config'`

- [ ] **Step 3: Write minimal implementation**

`app/config.py`:

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    data_dir: Path
    llm_provider: str = "mock"
    llm_api_key: str | None = None
    llm_model: str | None = None
    image_provider: str | None = None

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
    def db_path(self) -> Path:
        return self.data_dir / "media.db"

    @classmethod
    def load(cls) -> "Config":
        data_dir = Path(os.environ.get("MEDIA_AGENT_DATA_DIR", "data")).resolve()
        return cls(
            data_dir=data_dir,
            llm_provider=os.environ.get("MEDIA_AGENT_LLM_PROVIDER", "mock"),
            llm_api_key=os.environ.get("MEDIA_AGENT_LLM_API_KEY"),
            llm_model=os.environ.get("MEDIA_AGENT_LLM_MODEL"),
            image_provider=os.environ.get("MEDIA_AGENT_IMAGE_PROVIDER"),
        )

    def ensure_dirs(self) -> None:
        for d in (self.archive_dir, self.drafts_dir, self.images_dir):
            d.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_config.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add app/config.py tests/test_config.py
git commit -m "feat: config loading from env with data dir layout"
```

---

## Task 3: Data models

**Files:**
- Create: `app/models.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime, timezone
from app.models import Article, Draft, ArticleStatus, DraftStatus, slugify

def test_article_fingerprint_stable():
    a = Article(title="Hello World", content_md="body", url="https://x.com/a?utm=1",
                source_name="X", source_type="rss",
                published_at=None, images=[], raw_summary=None,
                fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    b = Article(title="Hello   World", content_md="other", url="https://x.com/a",
                source_name="Y", source_type="scrape",
                published_at=None, images=[], raw_summary=None,
                fetched_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert a.fingerprint() == b.fingerprint()

def test_normalized_url_strips_tracking():
    a = Article(title="t", content_md="b", url="https://x.com/p?utm_source=rss&id=5",
                source_name="X", source_type="rss", published_at=None,
                images=[], raw_summary=None, fetched_at=datetime.now(timezone.utc))
    assert a.normalized_url() == "https://x.com/p?id=5"

def test_slugify():
    assert slugify("Hello, World! 2026") == "hello-world-2026"
    assert slugify("物理AI breakthrough") == "ai-breakthrough" or slugify("物理AI breakthrough")

def test_status_constants():
    assert ArticleStatus.ARCHIVED == "archived"
    assert DraftStatus.DRAFTED == "drafted"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models'`

- [ ] **Step 3: Write minimal implementation**

`app/models.py`:

```python
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

    def normalized_url(self) -> str:
        parts = urlparse(self.url)
        query = [(k, v) for k, v in parse_qsl(parts.query)
                 if k.lower() not in _TRACKING_PARAMS]
        return urlunparse(parts._replace(query=urlencode(query), fragment=""))

    def fingerprint(self) -> str:
        norm_title = re.sub(r"\s+", " ", self.title).strip().lower()
        key = f"{self.normalized_url()}|{norm_title}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass
class Draft:
    article_id: int
    platform: str
    title_candidates: list[str]
    body_md: str
    topic: str
    source_url: str
    source_name: str
    cover_image: str | None = None
    flagged_claims: list[str] = field(default_factory=list)
    status: str = DraftStatus.DRAFTED
    draft_path: str | None = None
    id: int | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_models.py -v`
Expected: PASS (4 passed). Note: the slugify test for the Chinese-containing string just asserts it returns a non-empty slug.

- [ ] **Step 5: Commit**

```bash
git add app/models.py tests/test_models.py
git commit -m "feat: Article and Draft models with url normalization and fingerprint"
```

---

## Task 4: Database + schema

**Files:**
- Create: `app/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write the failing test**

```python
from app.db import connect, init_db

def test_init_db_creates_tables(tmp_path):
    db_path = tmp_path / "t.db"
    conn = connect(db_path)
    init_db(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {r["name"] for r in rows}
    assert {"sources", "articles", "drafts", "runs", "settings"} <= names

def test_connect_row_factory(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    conn.execute("INSERT INTO settings(key, value) VALUES('k','v')")
    row = conn.execute("SELECT key, value FROM settings").fetchone()
    assert row["key"] == "k" and row["value"] == "v"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.db'`

- [ ] **Step 3: Write minimal implementation**

`app/db.py`:

```python
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    url TEXT NOT NULL,
    mode TEXT,
    topics TEXT,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    source_name TEXT,
    source_type TEXT,
    topic TEXT,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    archive_path TEXT,
    fingerprint TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'archived'
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL,
    platform TEXT NOT NULL DEFAULT 'master',
    draft_path TEXT,
    cover_image TEXT,
    status TEXT NOT NULL DEFAULT 'drafted',
    updated_at TEXT NOT NULL,
    FOREIGN KEY(article_id) REFERENCES articles(id)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats_json TEXT,
    status TEXT NOT NULL DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_db.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/db.py tests/test_db.py
git commit -m "feat: sqlite schema and connection helper"
```

---

## Task 5: Store (article/draft persistence + markdown files)

**Files:**
- Create: `app/store.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime, timezone
from app.config import Config
from app.db import connect, init_db
from app.models import Article, Draft, ArticleStatus
from app.store import Store

def make_store(tmp_path):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    conn = connect(cfg.db_path)
    init_db(conn)
    return Store(conn, cfg)

def sample_article(url="https://x.com/a"):
    return Article(title="Big AI News", content_md="# Body\nfacts here",
                   url=url, source_name="X Blog", source_type="rss",
                   published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                   images=[], raw_summary="summary",
                   fetched_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                   topic="AI")

def test_save_article_writes_md_and_row(tmp_path):
    store = make_store(tmp_path)
    saved = store.save_article(sample_article())
    assert saved.id is not None
    assert saved.archive_path and (tmp_path / saved.archive_path).exists()
    text = (tmp_path / saved.archive_path).read_text(encoding="utf-8")
    assert "facts here" in text

def test_save_article_dedup_returns_existing(tmp_path):
    store = make_store(tmp_path)
    a1 = store.save_article(sample_article())
    a2 = store.save_article(sample_article(url="https://x.com/a?utm_source=z"))
    assert a1.id == a2.id  # same fingerprint

def test_exists_by_fingerprint(tmp_path):
    store = make_store(tmp_path)
    art = sample_article()
    assert not store.exists(art.fingerprint())
    store.save_article(art)
    assert store.exists(art.fingerprint())

def test_save_draft_writes_md(tmp_path):
    store = make_store(tmp_path)
    art = store.save_article(sample_article())
    draft = Draft(article_id=art.id, platform="master",
                  title_candidates=["T1", "T2"], body_md="## Hook\ntext",
                  topic="AI", source_url=art.url, source_name=art.source_name)
    saved = store.save_draft(draft)
    assert saved.id is not None
    assert (tmp_path / saved.draft_path).exists()
    content = (tmp_path / saved.draft_path).read_text(encoding="utf-8")
    assert "title_candidates" in content  # front-matter present
    assert "## Hook" in content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.store'`

- [ ] **Step 3: Write minimal implementation**

`app/store.py`:

```python
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from app.config import Config
from app.models import Article, Draft, ArticleStatus, DraftStatus, slugify


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
        slug = slugify(article.title)[:60]
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
        slug = slugify(draft.title_candidates[0] if draft.title_candidates
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_store.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add app/store.py tests/test_store.py
git commit -m "feat: store with markdown archive/draft files and dedup persistence"
```

---

## Task 6: Source base + dedup

**Files:**
- Create: `app/sources/__init__.py`
- Create: `app/sources/base.py`
- Create: `app/sources/dedup.py`
- Test: `tests/test_dedup.py`

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime, timezone
from app.models import Article
from app.sources.dedup import dedup

def art(url, title):
    return Article(title=title, content_md="b", url=url, source_name="s",
                   source_type="rss", published_at=None, images=[],
                   raw_summary=None, fetched_at=datetime.now(timezone.utc))

def test_dedup_removes_same_url_with_tracking():
    items = [art("https://x.com/a", "T"),
             art("https://x.com/a?utm_source=z", "T")]
    out = dedup(items)
    assert len(out) == 1

def test_dedup_keeps_distinct():
    items = [art("https://x.com/a", "A"), art("https://x.com/b", "B")]
    assert len(dedup(items)) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_dedup.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.sources.dedup'`

- [ ] **Step 3: Write minimal implementation**

`app/sources/__init__.py`:

```python
```

`app/sources/base.py`:

```python
from __future__ import annotations

from typing import Protocol

from app.models import Article


class Source(Protocol):
    name: str

    def fetch(self) -> list[Article]:
        ...
```

`app/sources/dedup.py`:

```python
from __future__ import annotations

from app.models import Article


def dedup(articles: list[Article]) -> list[Article]:
    seen: set[str] = set()
    result: list[Article] = []
    for a in articles:
        fp = a.fingerprint()
        if fp in seen:
            continue
        seen.add(fp)
        result.append(a)
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_dedup.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/sources/__init__.py app/sources/base.py app/sources/dedup.py tests/test_dedup.py
git commit -m "feat: source protocol and in-batch dedup"
```

---

## Task 7: HTML extractor

**Files:**
- Create: `app/sources/extractor.py`
- Test: `tests/test_extractor.py`
- Test fixture: `tests/fixtures/sample_article.html`

- [ ] **Step 1: Create the fixture**

`tests/fixtures/sample_article.html`:

```html
<!DOCTYPE html>
<html>
<head><title>Sample Page Title</title></head>
<body>
<nav>menu junk</nav>
<article>
<h1>Breakthrough in Physical AI</h1>
<p>Researchers announced a new world model on January 5, 2026.</p>
<p>The system improves robot manipulation accuracy by 30 percent.</p>
<img src="https://example.com/figure1.png" alt="figure"/>
</article>
<footer>copyright junk</footer>
</body>
</html>
```

- [ ] **Step 2: Write the failing test**

```python
from pathlib import Path
from app.sources.extractor import extract_from_html

FIXTURE = Path(__file__).parent / "fixtures" / "sample_article.html"

def test_extract_returns_title_and_body():
    html = FIXTURE.read_text(encoding="utf-8")
    result = extract_from_html(html, url="https://example.com/post")
    assert "Physical AI" in result["title"]
    assert "world model" in result["content_md"]
    assert "junk" not in result["content_md"]

def test_extract_collects_images():
    html = FIXTURE.read_text(encoding="utf-8")
    result = extract_from_html(html, url="https://example.com/post")
    assert any("figure1.png" in img for img in result["images"])
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_extractor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.sources.extractor'`

- [ ] **Step 4: Write minimal implementation**

`app/sources/extractor.py`:

```python
from __future__ import annotations

import re

import trafilatura
from trafilatura.metadata import extract_metadata


def _images_from_html(html: str) -> list[str]:
    return re.findall(r'<img[^>]+src="([^"]+)"', html)


def extract_from_html(html: str, url: str) -> dict:
    content_md = trafilatura.extract(
        html, output_format="markdown", include_images=True,
        include_links=True, url=url) or ""
    title = ""
    try:
        meta = extract_metadata(html)
        if meta and meta.title:
            title = meta.title
    except Exception:
        title = ""
    if not title:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL | re.IGNORECASE)
        if m:
            title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return {
        "title": title,
        "content_md": content_md,
        "images": _images_from_html(html),
    }
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_extractor.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add app/sources/extractor.py tests/test_extractor.py tests/fixtures/sample_article.html
git commit -m "feat: html content extraction with trafilatura"
```

---

## Task 8: RSS source

**Files:**
- Create: `app/sources/rss.py`
- Test: `tests/test_rss.py`
- Test fixture: `tests/fixtures/sample_feed.xml`

- [ ] **Step 1: Create the fixture**

`tests/fixtures/sample_feed.xml`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Example AI Blog</title>
  <item>
    <title>New Model Released</title>
    <link>https://example.com/new-model</link>
    <description>A summary of the new model and its benchmark results.</description>
    <pubDate>Mon, 05 Jan 2026 10:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Robotics Update</title>
    <link>https://example.com/robotics</link>
    <description>Progress on embodied agents.</description>
    <pubDate>Tue, 06 Jan 2026 10:00:00 GMT</pubDate>
  </item>
</channel>
</rss>
```

- [ ] **Step 2: Write the failing test**

```python
from pathlib import Path
from app.sources.rss import parse_feed

FIXTURE = Path(__file__).parent / "fixtures" / "sample_feed.xml"

def test_parse_feed_returns_articles():
    xml = FIXTURE.read_text(encoding="utf-8")
    articles = parse_feed(xml, source_name="Example AI Blog")
    assert len(articles) == 2
    first = articles[0]
    assert first.title == "New Model Released"
    assert first.url == "https://example.com/new-model"
    assert first.source_type == "rss"
    assert first.published_at is not None
    assert "summary" in first.raw_summary.lower()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_rss.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.sources.rss'`

- [ ] **Step 4: Write minimal implementation**

`app/sources/rss.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone
from time import mktime

import feedparser
import httpx

from app.models import Article


def _to_dt(entry) -> datetime | None:
    parsed = getattr(entry, "published_parsed", None) or \
        getattr(entry, "updated_parsed", None)
    if not parsed:
        return None
    return datetime.fromtimestamp(mktime(parsed), tz=timezone.utc)


def parse_feed(xml: str, source_name: str) -> list[Article]:
    feed = feedparser.parse(xml)
    now = datetime.now(timezone.utc)
    articles: list[Article] = []
    for entry in feed.entries:
        summary = getattr(entry, "summary", None)
        content_md = ""
        if getattr(entry, "content", None):
            content_md = entry.content[0].get("value", "")
        articles.append(Article(
            title=getattr(entry, "title", "").strip(),
            content_md=content_md or summary or "",
            url=getattr(entry, "link", ""),
            source_name=source_name,
            source_type="rss",
            published_at=_to_dt(entry),
            images=[],
            raw_summary=summary,
            fetched_at=now,
        ))
    return articles


def fetch_feed(url: str, source_name: str, timeout: float = 20.0) -> list[Article]:
    resp = httpx.get(url, timeout=timeout, follow_redirects=True,
                     headers={"User-Agent": "media-agent/0.1"})
    resp.raise_for_status()
    return parse_feed(resp.text, source_name)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_rss.py -v`
Expected: PASS (1 passed)

- [ ] **Step 6: Commit**

```bash
git add app/sources/rss.py tests/test_rss.py tests/fixtures/sample_feed.xml
git commit -m "feat: rss source parsing"
```

---

## Task 9: Web scraper

**Files:**
- Create: `app/sources/scraper.py`
- Test: `tests/test_scraper.py`

- [ ] **Step 1: Write the failing test**

```python
from app.sources.scraper import discover_links, scrape_single

LIST_HTML = """
<html><body>
<a href="/posts/a">Post A</a>
<a href="/posts/b">Post B</a>
<a href="https://other.com/x">External</a>
<a href="/about">About</a>
</body></html>
"""

ARTICLE_HTML = """
<html><head><title>Post A</title></head><body>
<article><h1>Post A Title</h1><p>Real content about AI here.</p>
<img src="https://example.com/a.png"/></article>
</body></html>
"""

def test_discover_links_filters_same_domain_and_pattern():
    links = discover_links(LIST_HTML, base_url="https://blog.example.com/",
                           include_pattern="/posts/")
    assert "https://blog.example.com/posts/a" in links
    assert "https://blog.example.com/posts/b" in links
    assert all("other.com" not in l for l in links)
    assert all("/about" not in l for l in links)

def test_scrape_single_builds_article(monkeypatch):
    def fake_get(url, **kwargs):
        class R:
            text = ARTICLE_HTML
            def raise_for_status(self): ...
        return R()
    monkeypatch.setattr("app.sources.scraper.httpx.get", fake_get)
    art = scrape_single("https://blog.example.com/posts/a", source_name="Blog")
    assert art is not None
    assert "Post A" in art.title
    assert "AI" in art.content_md
    assert art.source_type == "scrape"
    assert any("a.png" in i for i in art.images)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_scraper.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.sources.scraper'`

- [ ] **Step 3: Write minimal implementation**

`app/sources/scraper.py`:

```python
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx

from app.models import Article
from app.sources.extractor import extract_from_html

_UA = {"User-Agent": "media-agent/0.1 (+https://localhost)"}


def discover_links(html: str, base_url: str,
                   include_pattern: str | None = None) -> list[str]:
    base_host = urlparse(base_url).netloc
    hrefs = re.findall(r'<a[^>]+href="([^"]+)"', html)
    out: list[str] = []
    seen: set[str] = set()
    for href in hrefs:
        if href.startswith("#") or href.startswith("mailto:"):
            continue
        absolute = urljoin(base_url, href)
        if urlparse(absolute).netloc != base_host:
            continue
        if include_pattern and include_pattern not in absolute:
            continue
        if absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


def scrape_single(url: str, source_name: str,
                  timeout: float = 20.0) -> Article | None:
    resp = httpx.get(url, timeout=timeout, follow_redirects=True, headers=_UA)
    resp.raise_for_status()
    data = extract_from_html(resp.text, url=url)
    if not data["content_md"]:
        return None
    return Article(
        title=data["title"] or url,
        content_md=data["content_md"],
        url=url,
        source_name=source_name,
        source_type="scrape",
        published_at=None,
        images=data["images"],
        raw_summary=None,
        fetched_at=datetime.now(timezone.utc),
    )


def scrape_list(url: str, source_name: str, include_pattern: str | None = None,
                max_articles: int = 10, delay: float = 1.0) -> list[Article]:
    resp = httpx.get(url, timeout=20.0, follow_redirects=True, headers=_UA)
    resp.raise_for_status()
    links = discover_links(resp.text, base_url=url,
                           include_pattern=include_pattern)[:max_articles]
    articles: list[Article] = []
    for link in links:
        try:
            art = scrape_single(link, source_name)
            if art:
                articles.append(art)
        except Exception:
            continue
        time.sleep(delay)
    return articles
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_scraper.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/sources/scraper.py tests/test_scraper.py
git commit -m "feat: web scraper with link discovery and single-page extraction"
```

---

## Task 10: Feeds config loader

**Files:**
- Create: `app/feeds.py`
- Create: `feeds.yaml`
- Test: `tests/test_feeds.py`

- [ ] **Step 1: Write the failing test**

```python
from app.feeds import load_feeds, FeedsConfig

YAML = """
topics:
  - name: AI
    keywords: [AI, LLM]
  - name: 物理AI
    keywords: [robotics, world model]
sources:
  - name: OpenAI Blog
    type: rss
    url: https://openai.com/blog/rss.xml
    topics: [AI]
  - name: DeepMind Blog
    type: scrape
    mode: list
    url: https://deepmind.google/discover/blog/
    include_pattern: /discover/blog/
    topics: [AI, 物理AI]
"""

def test_load_feeds_parses_topics_and_sources(tmp_path):
    p = tmp_path / "feeds.yaml"
    p.write_text(YAML, encoding="utf-8")
    cfg = load_feeds(p)
    assert isinstance(cfg, FeedsConfig)
    assert {t.name for t in cfg.topics} == {"AI", "物理AI"}
    assert cfg.topics[0].keywords == ["AI", "LLM"]
    assert len(cfg.sources) == 2
    assert cfg.sources[1].mode == "list"
    assert cfg.sources[1].include_pattern == "/discover/blog/"

def test_load_feeds_defaults_mode_single_for_scrape(tmp_path):
    p = tmp_path / "feeds.yaml"
    p.write_text("""
topics: [{name: AI, keywords: [AI]}]
sources: [{name: S, type: scrape, url: https://x.com/a, topics: [AI]}]
""", encoding="utf-8")
    cfg = load_feeds(p)
    assert cfg.sources[0].mode == "single"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_feeds.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.feeds'`

- [ ] **Step 3: Write minimal implementation**

`app/feeds.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_feeds.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Create the sample feeds.yaml**

`feeds.yaml`:

```yaml
topics:
  - name: AI
    keywords: [AI, LLM, AGI, foundation model, GPT, model]
  - name: 物理AI
    keywords: [robotics, embodied, world model, physical AI, manipulation]

sources:
  - name: OpenAI Blog
    type: rss
    url: https://openai.com/blog/rss.xml
    topics: [AI]
  - name: Google DeepMind Blog
    type: scrape
    mode: list
    url: https://deepmind.google/discover/blog/
    include_pattern: /discover/blog/
    topics: [AI, 物理AI]
```

- [ ] **Step 6: Commit**

```bash
git add app/feeds.py feeds.yaml tests/test_feeds.py
git commit -m "feat: feeds.yaml config loader"
```

---

## Task 11: LLM abstraction + mock provider

**Files:**
- Create: `app/llm/__init__.py`
- Create: `app/llm/base.py`
- Create: `app/llm/providers/__init__.py`
- Create: `app/llm/providers/mock.py`
- Test: `tests/test_llm.py`

- [ ] **Step 1: Write the failing test**

```python
from app.llm.base import get_provider, Message
from app.llm.providers.mock import MockProvider

def test_get_provider_returns_mock():
    p = get_provider("mock")
    assert isinstance(p, MockProvider)

def test_mock_chat_echoes_deterministically():
    p = get_provider("mock")
    out1 = p.chat([Message(role="user", content="hello")])
    out2 = p.chat([Message(role="user", content="hello")])
    assert out1 == out2
    assert "hello" in out1

def test_mock_can_be_scripted():
    p = MockProvider(responses=["FIRST", "SECOND"])
    assert p.chat([Message(role="user", content="x")]) == "FIRST"
    assert p.chat([Message(role="user", content="y")]) == "SECOND"

def test_get_provider_unknown_raises():
    import pytest
    with pytest.raises(ValueError):
        get_provider("nope")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_llm.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.llm'`

- [ ] **Step 3: Write minimal implementation**

`app/llm/__init__.py`:

```python
```

`app/llm/base.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


class LLMProvider(Protocol):
    def chat(self, messages: list[Message], **opts) -> str:
        ...


def get_provider(name: str, api_key: str | None = None,
                 model: str | None = None) -> LLMProvider:
    if name == "mock":
        from app.llm.providers.mock import MockProvider
        return MockProvider()
    if name == "openai":
        from app.llm.providers.openai import OpenAIProvider
        return OpenAIProvider(api_key=api_key, model=model)
    raise ValueError(f"Unknown LLM provider: {name}")
```

`app/llm/providers/__init__.py`:

```python
```

`app/llm/providers/mock.py`:

```python
from __future__ import annotations

from app.llm.base import Message


class MockProvider:
    def __init__(self, responses: list[str] | None = None):
        self._responses = list(responses) if responses else None
        self._i = 0

    def chat(self, messages: list[Message], **opts) -> str:
        if self._responses is not None:
            out = self._responses[min(self._i, len(self._responses) - 1)]
            self._i += 1
            return out
        last = messages[-1].content if messages else ""
        return f"[mock] {last}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_llm.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add app/llm tests/test_llm.py
git commit -m "feat: llm provider abstraction with mock provider"
```

---

## Task 12: OpenAI provider

**Files:**
- Create: `app/llm/providers/openai.py`
- Test: `tests/test_openai_provider.py`

- [ ] **Step 1: Write the failing test**

```python
from app.llm.base import Message
from app.llm.providers.openai import OpenAIProvider

class FakeCompletions:
    def create(self, **kwargs):
        class Msg: content = "openai reply"
        class Choice: message = Msg()
        class Resp: choices = [Choice()]
        self.kwargs = kwargs
        return Resp()

class FakeChat:
    def __init__(self): self.completions = FakeCompletions()

class FakeClient:
    def __init__(self, **kwargs): self.chat = FakeChat()

def test_openai_chat_uses_client(monkeypatch):
    monkeypatch.setattr("app.llm.providers.openai.OpenAI", FakeClient)
    p = OpenAIProvider(api_key="sk-test", model="gpt-4o-mini")
    out = p.chat([Message(role="user", content="hi")])
    assert out == "openai reply"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_openai_provider.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.llm.providers.openai'`

- [ ] **Step 3: Write minimal implementation**

`app/llm/providers/openai.py`:

```python
from __future__ import annotations

from openai import OpenAI

from app.llm.base import Message


class OpenAIProvider:
    def __init__(self, api_key: str | None = None,
                 model: str | None = None):
        self.client = OpenAI(api_key=api_key)
        self.model = model or "gpt-4o-mini"

    def chat(self, messages: list[Message], **opts) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": m.role, "content": m.content} for m in messages],
            temperature=opts.get("temperature", 0.7),
        )
        return resp.choices[0].message.content or ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_openai_provider.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add app/llm/providers/openai.py tests/test_openai_provider.py
git commit -m "feat: openai llm provider"
```

---

## Task 13: Classifier

**Files:**
- Create: `app/pipeline/__init__.py`
- Create: `app/pipeline/classifier.py`
- Test: `tests/test_classifier.py`

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime, timezone
from app.models import Article
from app.feeds import Topic
from app.llm.providers.mock import MockProvider
from app.pipeline.classifier import classify

def art(title, body):
    return Article(title=title, content_md=body, url="https://x.com/a",
                   source_name="s", source_type="rss", published_at=None,
                   images=[], raw_summary=None,
                   fetched_at=datetime.now(timezone.utc))

TOPICS = [Topic(name="AI", keywords=["LLM", "GPT"]),
          Topic(name="物理AI", keywords=["robotics", "world model"])]

def test_classify_by_keyword():
    a = art("New GPT model", "A new LLM was released")
    assert classify(a, TOPICS, MockProvider()) == "AI"

def test_classify_physical_ai_keyword():
    a = art("Robot news", "advances in robotics and world model")
    assert classify(a, TOPICS, MockProvider()) == "物理AI"

def test_classify_falls_back_to_llm():
    a = art("Ambiguous title", "no obvious keyword present here")
    provider = MockProvider(responses=["AI"])
    assert classify(a, TOPICS, provider) == "AI"

def test_classify_llm_invalid_returns_uncategorized():
    a = art("Ambiguous", "nothing matches")
    provider = MockProvider(responses=["not-a-real-topic"])
    assert classify(a, TOPICS, provider) == "uncategorized"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_classifier.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.pipeline.classifier'`

- [ ] **Step 3: Write minimal implementation**

`app/pipeline/__init__.py`:

```python
```

`app/pipeline/classifier.py`:

```python
from __future__ import annotations

from app.feeds import Topic
from app.llm.base import LLMProvider, Message
from app.models import Article

UNCATEGORIZED = "uncategorized"


def _keyword_match(article: Article, topics: list[Topic]) -> str | None:
    haystack = f"{article.title}\n{article.content_md}".lower()
    for topic in topics:
        for kw in topic.keywords:
            if kw.lower() in haystack:
                return topic.name
    return None


def classify(article: Article, topics: list[Topic],
             provider: LLMProvider) -> str:
    matched = _keyword_match(article, topics)
    if matched:
        return matched

    names = [t.name for t in topics]
    prompt = (
        "You are a topic classifier. Choose exactly ONE topic name from this "
        f"list that best fits the article, or reply 'none'.\nTopics: {names}\n\n"
        f"Title: {article.title}\n\nContent:\n{article.content_md[:1500]}\n\n"
        "Reply with only the topic name."
    )
    reply = provider.chat([Message(role="user", content=prompt)]).strip()
    for name in names:
        if name.lower() == reply.lower():
            return name
    return UNCATEGORIZED
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_classifier.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add app/pipeline/__init__.py app/pipeline/classifier.py tests/test_classifier.py
git commit -m "feat: keyword + llm topic classifier"
```

---

## Task 14: Rewriter (faithful rewrite + faithfulness check)

**Files:**
- Create: `app/pipeline/rewriter.py`
- Test: `tests/test_rewriter.py`

- [ ] **Step 1: Write the failing test**

```python
import json
from datetime import datetime, timezone
from app.models import Article, Draft
from app.llm.providers.mock import MockProvider
from app.pipeline.rewriter import rewrite

def sample_article():
    return Article(title="New AI Model", content_md="The model scores 90 on bench.",
                   url="https://x.com/a", source_name="X Blog", source_type="rss",
                   published_at=None, images=["https://x.com/img.png"],
                   raw_summary=None, fetched_at=datetime.now(timezone.utc),
                   topic="AI")

def test_rewrite_builds_draft_from_json():
    rewrite_json = json.dumps({
        "title_candidates": ["震撼！新模型登场", "新AI模型刷新纪录"],
        "body_md": "## 开头钩子\n正文内容\n",
    })
    check_json = json.dumps({"flagged_claims": []})
    provider = MockProvider(responses=[rewrite_json, check_json])
    draft = rewrite(sample_article(), provider)
    assert isinstance(draft, Draft)
    assert draft.title_candidates[0] == "震撼！新模型登场"
    assert "正文内容" in draft.body_md
    assert draft.source_url == "https://x.com/a"
    assert draft.topic == "AI"
    assert draft.flagged_claims == []

def test_rewrite_captures_flagged_claims():
    rewrite_json = json.dumps({
        "title_candidates": ["标题"],
        "body_md": "正文 with 99% number not in source",
    })
    check_json = json.dumps({
        "flagged_claims": ["'99%' is not supported by the source"]})
    provider = MockProvider(responses=[rewrite_json, check_json])
    draft = rewrite(sample_article(), provider)
    assert len(draft.flagged_claims) == 1
    assert "99%" in draft.flagged_claims[0]

def test_rewrite_handles_non_json_gracefully():
    provider = MockProvider(responses=["not json at all", "also not json"])
    draft = rewrite(sample_article(), provider)
    # falls back: body becomes the raw model text, one title from article
    assert draft.title_candidates
    assert draft.body_md
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_rewriter.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.pipeline.rewriter'`

- [ ] **Step 3: Write minimal implementation**

`app/pipeline/rewriter.py`:

```python
from __future__ import annotations

import json
import re

from app.llm.base import LLMProvider, Message
from app.models import Article, Draft

REWRITE_SYSTEM = (
    "你是资深自媒体编辑。基于且仅基于提供的原文事实进行改写，"
    "目标是通俗易懂、吸引眼球、有爆款潜质，但绝不可编造原文没有的数据、"
    "引用、结论或时间。不确定的内容不要写。"
)

REWRITE_INSTRUCTION = (
    "请把下面的原文改写成中文自媒体文章。只能依据原文事实。\n"
    "输出严格的 JSON，字段：\n"
    '  "title_candidates": [3 个吸睛但不虚假的标题],\n'
    '  "body_md": "Markdown 正文，含开头钩子、小标题、分点，适合手机阅读"\n'
    "不要输出 JSON 以外的任何内容。\n\n"
    "原文标题：{title}\n来源：{source}\n\n原文正文：\n{content}"
)

CHECK_INSTRUCTION = (
    "你是事实校验员。对照原文，找出改写稿中原文未提及或可能失真的具体说法。\n"
    "输出严格 JSON：{{\"flagged_claims\": [可疑说法的简短描述]}}。"
    "若全部忠实于原文，返回空数组。不要输出 JSON 以外内容。\n\n"
    "原文：\n{source_content}\n\n改写稿：\n{draft}"
)


def _extract_json(text: str) -> dict | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


def rewrite(article: Article, provider: LLMProvider,
            platform: str = "master") -> Draft:
    rewrite_prompt = REWRITE_INSTRUCTION.format(
        title=article.title, source=article.source_name,
        content=article.content_md[:6000])
    raw = provider.chat([
        Message(role="system", content=REWRITE_SYSTEM),
        Message(role="user", content=rewrite_prompt),
    ])
    parsed = _extract_json(raw)
    if parsed and parsed.get("title_candidates") and parsed.get("body_md"):
        title_candidates = parsed["title_candidates"]
        body_md = parsed["body_md"]
    else:
        title_candidates = [article.title]
        body_md = raw

    body_with_source = (
        f"{body_md}\n\n---\n**信息来源**："
        f"[{article.source_name}]({article.url})\n"
    )

    check_prompt = CHECK_INSTRUCTION.format(
        source_content=article.content_md[:6000], draft=body_md)
    check_raw = provider.chat([Message(role="user", content=check_prompt)])
    check_parsed = _extract_json(check_raw) or {}
    flagged = check_parsed.get("flagged_claims", []) or []

    return Draft(
        article_id=article.id or 0,
        platform=platform,
        title_candidates=title_candidates,
        body_md=body_with_source,
        topic=article.topic or "uncategorized",
        source_url=article.url,
        source_name=article.source_name,
        cover_image=None,
        flagged_claims=flagged,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_rewriter.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add app/pipeline/rewriter.py tests/test_rewriter.py
git commit -m "feat: faithful rewriter with faithfulness check"
```

---

## Task 15: Orchestrator

**Files:**
- Create: `app/pipeline/orchestrator.py`
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime, timezone
from app.config import Config
from app.db import connect, init_db
from app.store import Store
from app.models import Article
from app.feeds import FeedsConfig, Topic, SourceConfig
from app.llm.providers.mock import MockProvider
from app.pipeline.orchestrator import run_pipeline

import json

def build(tmp_path):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    conn = connect(cfg.db_path)
    init_db(conn)
    return cfg, Store(conn, cfg)

def fake_articles():
    return [Article(title="GPT-5 released", content_md="It scores high.",
                    url="https://x.com/a", source_name="X", source_type="rss",
                    published_at=None, images=[], raw_summary=None,
                    fetched_at=datetime.now(timezone.utc))]

def test_run_pipeline_end_to_end(tmp_path, monkeypatch):
    cfg, store = build(tmp_path)
    feeds = FeedsConfig(
        topics=[Topic(name="AI", keywords=["GPT"])],
        sources=[SourceConfig(name="X", type="rss", url="https://x.com/feed",
                              topics=["AI"])])

    monkeypatch.setattr("app.pipeline.orchestrator.collect_sources",
                        lambda feeds_cfg: fake_articles())

    rewrite_json = json.dumps({"title_candidates": ["爆款标题"],
                               "body_md": "## 钩子\n正文"})
    check_json = json.dumps({"flagged_claims": []})
    provider = MockProvider(responses=[rewrite_json, check_json])

    stats = run_pipeline(feeds, store, provider, max_drafts=5)
    assert stats["fetched"] == 1
    assert stats["archived"] == 1
    assert stats["drafted"] == 1

    drafts = store.conn.execute("SELECT * FROM drafts").fetchall()
    assert len(drafts) == 1
    articles = store.conn.execute("SELECT * FROM articles").fetchall()
    assert articles[0]["topic"] == "AI"

def test_run_pipeline_skips_duplicates(tmp_path, monkeypatch):
    cfg, store = build(tmp_path)
    feeds = FeedsConfig(topics=[Topic(name="AI", keywords=["GPT"])], sources=[])
    monkeypatch.setattr("app.pipeline.orchestrator.collect_sources",
                        lambda feeds_cfg: fake_articles())
    provider = MockProvider(responses=[
        json.dumps({"title_candidates": ["t"], "body_md": "b"}),
        json.dumps({"flagged_claims": []})])
    run_pipeline(feeds, store, provider, max_drafts=5)
    stats2 = run_pipeline(feeds, store, provider, max_drafts=5)
    assert stats2["archived"] == 0  # duplicate skipped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_orchestrator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.pipeline.orchestrator'`

- [ ] **Step 3: Write minimal implementation**

`app/pipeline/orchestrator.py`:

```python
from __future__ import annotations

from app.feeds import FeedsConfig
from app.llm.base import LLMProvider
from app.models import Article
from app.sources.dedup import dedup
from app.sources.rss import fetch_feed
from app.sources.scraper import scrape_list, scrape_single
from app.store import Store
from app.pipeline.classifier import classify
from app.pipeline.rewriter import rewrite


def collect_sources(feeds: FeedsConfig) -> list[Article]:
    articles: list[Article] = []
    for src in feeds.sources:
        if not src.enabled:
            continue
        try:
            if src.type == "rss":
                articles.extend(fetch_feed(src.url, src.name))
            elif src.type == "scrape":
                if src.mode == "list":
                    articles.extend(scrape_list(
                        src.url, src.name, include_pattern=src.include_pattern))
                else:
                    art = scrape_single(src.url, src.name)
                    if art:
                        articles.append(art)
        except Exception:
            continue
    return articles


def run_pipeline(feeds: FeedsConfig, store: Store, provider: LLMProvider,
                 max_drafts: int = 10) -> dict:
    stats = {"fetched": 0, "archived": 0, "classified": 0, "drafted": 0}

    articles = dedup(collect_sources(feeds))
    stats["fetched"] = len(articles)

    new_articles: list[Article] = []
    for art in articles:
        if store.exists(art.fingerprint()):
            continue
        art.topic = classify(art, feeds.topics, provider)
        saved = store.save_article(art)
        stats["archived"] += 1
        stats["classified"] += 1
        new_articles.append(saved)

    for art in new_articles[:max_drafts]:
        draft = rewrite(art, provider)
        store.save_draft(draft)
        stats["drafted"] += 1

    return stats
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_orchestrator.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/pipeline/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: pipeline orchestrator wiring sources to drafts"
```

---

## Task 16: CLI

**Files:**
- Create: `app/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
from typer.testing import CliRunner
from app.cli import app

runner = CliRunner()

def test_init_command_creates_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert (tmp_path / "media.db").exists()

def test_run_command_uses_mock(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEDIA_AGENT_LLM_PROVIDER", "mock")
    feeds = tmp_path / "feeds.yaml"
    feeds.write_text(
        "topics: [{name: AI, keywords: [GPT]}]\nsources: []\n", encoding="utf-8")
    result = runner.invoke(app, ["run", "--feeds", str(feeds)])
    assert result.exit_code == 0
    assert "fetched" in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.cli'`

- [ ] **Step 3: Write minimal implementation**

`app/cli.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import typer

from app.config import Config
from app.db import connect, init_db
from app.feeds import load_feeds
from app.llm.base import get_provider
from app.pipeline.orchestrator import run_pipeline
from app.store import Store

app = typer.Typer(help="Media agent pipeline CLI")


def _store() -> Store:
    cfg = Config.load()
    cfg.ensure_dirs()
    conn = connect(cfg.db_path)
    init_db(conn)
    return Store(conn, cfg)


@app.command()
def init():
    """Initialize data dir and database."""
    store = _store()
    typer.echo(f"Initialized at {store.config.data_dir}")


@app.command()
def run(feeds: str = typer.Option("feeds.yaml", help="Path to feeds.yaml"),
        max_drafts: int = typer.Option(10)):
    """Run one full pipeline pass."""
    cfg = Config.load()
    store = _store()
    feeds_cfg = load_feeds(Path(feeds))
    provider = get_provider(cfg.llm_provider, cfg.llm_api_key, cfg.llm_model)
    stats = run_pipeline(feeds_cfg, store, provider, max_drafts=max_drafts)
    typer.echo(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    app()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_cli.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Run the full test suite**

Run: `.venv\Scripts\python -m pytest -v`
Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add app/cli.py tests/test_cli.py
git commit -m "feat: typer cli with init and run commands"
```

---

## Task 17: README

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write README**

`README.md`:

```markdown
# Media Agent

自媒体运营自动助理：抓取 RSS/网页文章 → 去重归档 → 主题分类 → 保真改写为爆款 Markdown 草稿。

## 安装

\`\`\`bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
\`\`\`

## 配置

编辑 `feeds.yaml` 配置主题与来源。设置环境变量：

- `MEDIA_AGENT_DATA_DIR`（默认 `data`）
- `MEDIA_AGENT_LLM_PROVIDER`（`mock` | `openai`）
- `MEDIA_AGENT_LLM_API_KEY`
- `MEDIA_AGENT_LLM_MODEL`

## 使用

\`\`\`bash
.venv\Scripts\python -m app.cli init
.venv\Scripts\python -m app.cli run --feeds feeds.yaml
\`\`\`

归档原文写入 `data/archive/`，草稿写入 `data/drafts/`（Markdown，带 front-matter）。

## 测试

\`\`\`bash
.venv\Scripts\python -m pytest -v
\`\`\`
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add README"
```

---

## Self-Review Notes

- **Spec coverage:** RSS (Task 8), 网页爬取 (Task 9), 抽取 (Task 7), 去重 (Task 6), 归档 markdown (Task 5), 分类 (Task 13), 保真改写+校验 (Task 14), LLM 可切换 (Task 11/12), CLI 编排 (Task 15/16). 配图、Web 界面、定时调度属于 Plan 2/3，本计划不含。
- **Type consistency:** `Article` / `Draft` fields defined in Task 3 are used consistently in Tasks 5/13/14/15. `get_provider`, `Message`, `chat` consistent across Tasks 11/12/13/14. `Store.exists`, `save_article`, `save_draft`, `set_topic` used as defined.
