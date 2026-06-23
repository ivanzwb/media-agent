# Media Agent — Plan 2: Images + Local Web UI

> **For agentic workers:** Implement task-by-task with TDD. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add image handling (download original article images, AI-generated cover with mock fallback) and a local FastAPI web UI to browse archives, review/edit Markdown drafts, configure sources/topics, and trigger pipeline runs.

**Architecture:** Extend the existing `app/` package. Add an image-provider abstraction parallel to the LLM layer. Add `Store` query/update methods needed by the UI. Build a FastAPI app (`app/web/server.py`) with Jinja2 server-rendered pages plus a few JSON endpoints; the draft editor uses a CDN Markdown editor (EasyMDE) with live preview. Runs are triggered synchronously for the MVP.

**Tech Stack (added):** fastapi, uvicorn[standard], jinja2, python-multipart, pillow, httpx (already present), openai (already present, for image generation).

---

## File Structure

- Modify: `requirements.txt` — add fastapi, uvicorn, jinja2, python-multipart, pillow
- Create: `app/images/__init__.py`
- Create: `app/images/base.py` — `ImageProvider` protocol + `get_image_provider()`
- Create: `app/images/providers/__init__.py`
- Create: `app/images/providers/mock.py` — generates a solid-color PNG placeholder (pillow)
- Create: `app/images/providers/openai_images.py` — DALL·E/gpt-image cover generation
- Create: `app/pipeline/images.py` — `download_original_images()`, `attach_cover()`
- Modify: `app/pipeline/orchestrator.py` — call image step after rewrite; record cover on draft
- Modify: `app/store.py` — add: `list_articles`, `get_article`, `list_drafts`, `get_draft`, `read_draft_body`, `update_draft_body`, `set_draft_status`, `list_runs`, `record_run`, `dashboard_stats`, `set_draft_cover`
- Modify: `app/models.py` — add `DraftRecord`/`ArticleRecord` light read structs OR reuse Row dicts (decision: return sqlite Row-backed dicts for reads)
- Create: `app/web/__init__.py`
- Create: `app/web/server.py` — FastAPI app factory `create_app()`
- Create: `app/web/templates/base.html`
- Create: `app/web/templates/dashboard.html`
- Create: `app/web/templates/archive.html`
- Create: `app/web/templates/draft_edit.html`
- Create: `app/web/templates/sources.html`
- Create: `app/web/templates/settings.html`
- Create: `app/web/static/app.css`
- Modify: `app/cli.py` — add `serve` command (uvicorn)
- Modify: `app/feeds.py` — add `save_feeds()` to persist edits back to YAML
- Tests under `tests/`

---

## Design Decisions

- **Reads return dict-like rows.** UI list/detail methods return `sqlite3.Row` (dict-accessible). Avoids a parallel ORM. Draft body is read from the Markdown file via python-frontmatter.
- **Image provider mirrors LLM provider.** `get_image_provider(name)` with `mock` (pillow placeholder, default, offline) and `openai` (real). Selected via `Config.image_provider` / env `MEDIA_AGENT_IMAGE_PROVIDER`.
- **Cover always generated.** Pipeline calls `attach_cover` using the image provider; original inline images downloaded best-effort. Failures are non-fatal.
- **Draft editing writes back to the same Markdown file** (front-matter preserved/updated). DB `drafts.status`, `cover_image`, `updated_at` updated.
- **Runs are synchronous** for MVP; `record_run` writes start/finish/stats.
- **Suppress git author warning** is out of scope; not touched.

---

## Task 1: Add dependencies

**Files:** Modify `requirements.txt`

- [ ] Append:

```
fastapi==0.115.5
uvicorn[standard]==0.32.1
jinja2==3.1.4
python-multipart==0.0.12
pillow==11.0.0
```

- [ ] Install: `.\.venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt`
- [ ] Commit.

---

## Task 2: Image provider abstraction + mock

**Files:** Create `app/images/__init__.py`, `app/images/base.py`, `app/images/providers/__init__.py`, `app/images/providers/mock.py`; Test `tests/test_images_provider.py`

- [ ] **Test first:**

```python
from pathlib import Path
from app.images.base import get_image_provider
from app.images.providers.mock import MockImageProvider

def test_get_image_provider_mock():
    assert isinstance(get_image_provider("mock"), MockImageProvider)

def test_mock_generates_png(tmp_path):
    p = get_image_provider("mock")
    out = p.generate(prompt="cover for AI article", out_path=tmp_path / "c.png")
    assert Path(out).exists()
    assert Path(out).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

def test_get_image_provider_unknown_raises():
    import pytest
    with pytest.raises(ValueError):
        get_image_provider("nope")
```

- [ ] **Implement** `base.py`:

```python
from __future__ import annotations
from pathlib import Path
from typing import Protocol

class ImageProvider(Protocol):
    def generate(self, prompt: str, out_path: Path) -> Path: ...

def get_image_provider(name: str, api_key: str | None = None,
                       model: str | None = None) -> "ImageProvider":
    if name in ("mock", None, ""):
        from app.images.providers.mock import MockImageProvider
        return MockImageProvider()
    if name == "openai":
        from app.images.providers.openai_images import OpenAIImageProvider
        return OpenAIImageProvider(api_key=api_key, model=model)
    raise ValueError(f"Unknown image provider: {name}")
```

- [ ] **Implement** `mock.py` (pillow solid color + text):

```python
from __future__ import annotations
from pathlib import Path
from PIL import Image, ImageDraw

class MockImageProvider:
    def generate(self, prompt: str, out_path: Path) -> Path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (1024, 576), (32, 80, 160))
        draw = ImageDraw.Draw(img)
        draw.text((40, 40), prompt[:60], fill=(255, 255, 255))
        img.save(out_path, format="PNG")
        return out_path
```

- [ ] Run tests, commit.

---

## Task 3: OpenAI image provider

**Files:** Create `app/images/providers/openai_images.py`; Test `tests/test_openai_images.py` (mock client)

- [ ] **Test first** (monkeypatch `OpenAI`, fake `images.generate` returning b64), assert PNG written.
- [ ] **Implement** using `client.images.generate(model=..., prompt=..., size="1024x1024", response_format="b64_json")`, decode base64, write file.
- [ ] Run tests, commit.

---

## Task 4: Pipeline images

**Files:** Create `app/pipeline/images.py`; Test `tests/test_pipeline_images.py`

- [ ] **Test first:**
  - `download_original_images(urls, dest_dir, fetch=fake)` writes files for each successful fetch, skips failures, returns local relative paths.
  - `attach_cover(draft, image_provider, images_dir)` sets `draft.cover_image` to a relative path and the file exists; returns draft.

- [ ] **Implement:**

```python
from __future__ import annotations
from pathlib import Path
import httpx
from app.images.base import ImageProvider
from app.models import Draft, slugify

def download_original_images(urls, dest_dir, fetch=None) -> list[str]:
    dest_dir = Path(dest_dir); dest_dir.mkdir(parents=True, exist_ok=True)
    fetch = fetch or (lambda u: httpx.get(u, timeout=20.0, follow_redirects=True).content)
    saved = []
    for i, url in enumerate(urls):
        try:
            data = fetch(url)
        except Exception:
            continue
        ext = ".jpg" if ".jpg" in url.lower() else ".png"
        p = dest_dir / f"img-{i}{ext}"
        p.write_bytes(data)
        saved.append(p.name)
    return saved

def attach_cover(draft: Draft, provider: ImageProvider, images_dir) -> Draft:
    images_dir = Path(images_dir)
    name = f"cover-{slugify(draft.title_candidates[0])[:40]}.png"
    out = images_dir / name
    try:
        provider.generate(prompt=f"科技自媒体封面：{draft.title_candidates[0]}", out_path=out)
        draft.cover_image = name
    except Exception:
        draft.cover_image = None
    return draft
```

- [ ] Run tests, commit.

---

## Task 5: Store query/update methods

**Files:** Modify `app/store.py`; Test extend `tests/test_store.py`

- [ ] **Tests first** for: `list_articles(limit, topic=None)`, `get_article(id)`, `list_drafts(status=None)`, `get_draft(id)`, `read_draft_body(id)`, `update_draft_body(id, title_candidates, body_md, status)`, `set_draft_status(id, status)`, `set_draft_cover(id, cover)`, `record_run()/finish_run()`, `list_runs()`, `dashboard_stats()`.
- [ ] **Implement** methods. `read_draft_body` loads the markdown file via `frontmatter.load`; `update_draft_body` rewrites file front-matter + body and updates DB row (`status`, `updated_at`).
- [ ] Run tests, commit.

---

## Task 6: Orchestrator integration + feeds.save

**Files:** Modify `app/pipeline/orchestrator.py`, `app/feeds.py`; Test extend `tests/test_orchestrator.py`, `tests/test_feeds.py`

- [ ] `run_pipeline(..., image_provider=None, download_images=False)`: after `save_draft`, call `attach_cover` when an image provider is given, then `store.set_draft_cover`. Record a run row with stats.
- [ ] `feeds.save_feeds(config, path)` writes topics/sources back to YAML; round-trip test (`load_feeds(save_feeds(cfg))` equals cfg).
- [ ] Run tests, commit.

---

## Task 7: FastAPI app + dashboard

**Files:** Create `app/web/__init__.py`, `app/web/server.py`, `templates/base.html`, `templates/dashboard.html`, `static/app.css`; Test `tests/test_web.py` (FastAPI TestClient)

- [ ] **Test first:** `create_app()` returns app; `GET /` returns 200 and contains "Media Agent"; `GET /api/stats` returns JSON with keys `articles`, `drafts`, `runs`.
- [ ] **Implement** `create_app()` building a `Store` from `Config`, Jinja2 templates, static mount, dashboard route, `/api/stats`.
- [ ] Run tests, commit.

---

## Task 8: Archive browsing

**Files:** Modify `app/web/server.py`; Create `templates/archive.html`; Test extend `tests/test_web.py`

- [ ] **Test first:** seed an article via Store; `GET /archive` returns 200 and shows the article title; `GET /archive?topic=AI` filters.
- [ ] **Implement** route using `list_articles`. Render table with title/source/topic/date and link to source.
- [ ] Run tests, commit.

---

## Task 9: Draft review + Markdown editor

**Files:** Modify `app/web/server.py`; Create `templates/draft_edit.html`; Test extend `tests/test_web.py`

- [ ] **Test first:**
  - `GET /drafts` lists drafts.
  - `GET /drafts/{id}/edit` returns 200, contains the draft body text and a `<textarea>`.
  - `POST /drafts/{id}` with form fields (`title_candidates` newline-separated, `body_md`, `status`) updates the file + DB; redirect 303; reloading shows new body.
- [ ] **Implement** edit page: EasyMDE (CDN) bound to textarea, live preview, title-candidates textarea, status select, flagged-claims shown as a warning banner if present, cover image preview. Save posts form to `POST /drafts/{id}`.
- [ ] Run tests, commit.

---

## Task 10: Sources/topics config + run trigger + settings

**Files:** Modify `app/web/server.py`, `app/cli.py`; Create `templates/sources.html`, `templates/settings.html`; Test extend `tests/test_web.py`

- [ ] **Test first:**
  - `GET /sources` shows existing sources from feeds.yaml.
  - `POST /sources/add` appends a source and persists via `save_feeds`; redirect.
  - `POST /run` triggers `run_pipeline` with mock providers and redirects to dashboard (use mock LLM + mock images in test via env).
  - `GET /settings` returns 200.
- [ ] **Implement** routes; add CLI `serve` command: `uvicorn.run(create_app(), host, port)`.
- [ ] Run full suite, manual smoke test (`python -m app.cli serve`), commit.

---

## Self-Review Checklist

- Spec coverage: 配图(原图下载 Task4 + AI 封面 Task2/3/4)、Web 界面(仪表盘 Task7 / 归档浏览 Task8 / 草稿 md 编辑审核 Task9 / 来源配置 Task10 / 设置 Task10 / 手动运行 Task10).
- Type consistency: `get_image_provider`/`ImageProvider.generate(prompt, out_path)`, `Draft.cover_image`, Store method names match across tasks.
- Deferred to Plan 3: 定时调度(APScheduler)、运行控制(按时间窗/每源条数过滤)、平台适配改写。
