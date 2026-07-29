from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Query, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles


from app.config import Config
from app.db import connect, init_db
from app.discovery import discover_from_url, discover_from_keyword
from app.feeds import (
    check_url_connectivity, load_feeds, save_feeds, update_feeds,
    SourceConfig, Topic, FeedsConfig,
)
from app.images.base import get_image_provider
from app.llm.base import get_provider, get_rewrite_provider, Message, LLMProvider
from app.llm.providers import cli as cli_provider
from app.models import Article, Draft
from app.models import slugify as _slugify
from app.pipeline.adapter import adapt, PLATFORMS
from app.platforms.registry import get as get_platform, list_all as list_platforms
from app.pipeline.images import attach_cover
from app.pipeline.narration import (
    generate_narration, load_narration, save_scenes, resynth_scenes)
from app.pipeline.orchestrator import run_pipeline, _append_prompt
from app.pipeline.recommender import (
    suggest_subtopics, suggest_keywords, suggest_sources, compute_hotness,
    suggest_keywords_batch, suggest_source_names_batch, _resolve_sources,
    _SOURCE_BATCH_CHUNK)
from app.pipeline.score import compute_draft_score
from app.pipeline.localize import (
    localize_one, localize_article, content_images, content_videos)
from app.pipeline.rewriter import rewrite
from app.pipeline.search_create import (
    SearchCreateCancelled, SearchCreateOptions, run_search_create)
from app.pipeline import styles as rewrite_styles
from app.wechat import components as editor_components
from app.pipeline.sanitizer import load_words, sanitize_draft
from app.pipeline.video import build_explainer_video, video_path
from app.sources.extractor import _images_from_html, _videos_from_html
from app.tts.base import get_tts_provider
from app.tts.voices import (
    list_voices, add_voice, delete_voice, sample_path as voice_sample_path)
from app.scheduler import start_if_enabled
from app.store import Store
from app.licensing import LicenseManager
from app.licensing import features as LF, gates as LG

_BASE = Path(__file__).parent
_STATIC = _BASE / "static"
# Built React SPA (frontend/dist). When present it serves the UI;
# otherwise a placeholder is displayed asking to build the frontend.
_SPA_DIST = _BASE.parent.parent / "frontend" / "dist"

logger = logging.getLogger(__name__)


def _to_int(value: str, default: int) -> int:
    try:
        n = int(str(value).strip())
        return n if n > 0 else default
    except (ValueError, TypeError):
        return default


_THINKING_PATTERNS: tuple[str, ...] = (
    # English first-person planning / narrative — never part of a real article body
    "i detect ", "detect implementation", "let me ", "my approach",
    "now i ", "now let me", "here is my plan", "i will search",
    "i'll search", "i will fetch", "i'll fetch", "let's search",
    "i have enough information",
    # Chinese thinking/summary markers
    "已完成", "以下是为", "我将", "我来", "文件已写入",
    "已写入", "已保存到", "output.md", "文件已保存",
)
"""Known agent thinking/planning/summary phrases that should never appear
in a real article body.  Used by ``_clean_agent_output`` to detect when
the agent reasoning leaked into its output."""


def _looks_like_thinking_text(body: str) -> bool:
    """Check if *body* appears to be agent thinking/planning, not article content.

    Heuristic: the body starts with a known thinking pattern on its first
    non-blank line, OR lacks any markdown headings (``##``) and is very
    short (<10 lines), which suggests it's a summary/report rather than
    a real article body.
    """
    stripped = body.strip()
    if not stripped:
        return False

    first_line = stripped.split("\n", 1)[0].strip().lower()
    for pat in _THINKING_PATTERNS:
        if first_line.startswith(pat.lower()):
            return True

    # No markdown headings and very short → likely summary/report
    lines = [l for l in stripped.split("\n") if l.strip()]
    if len(lines) < 10 and "## " not in stripped:
        return True

    return False


def _clean_agent_output(text: str, original_body: str | None = None) -> str:
    """Sanitize raw agent output into valid front-matter markdown.

    Agents frequently disobey instructions and include:
    - Thinking/commentary text before the front-matter
    - Code block fences (```markdown … ```)
    - Trailing modification notes after the body

    When *original_body* is provided and the cleaned body is detected as
    thinking text, the original body is preserved as a fallback so the
    draft isn't corrupted.
    """
    import frontmatter as _fm
    if not text or not text.strip():
        return ""
    cleaned = text.strip()

    # 1. Strip leading garbage before the first front-matter delimiter
    #    (agents often put thinking text before ---)
    if not cleaned.startswith("---"):
        idx = cleaned.find("\n---")
        if idx != -1:
            cleaned = cleaned[idx:].strip()

    # 2. Unwrap markdown code fences if present
    #    ```markdown\n...\n```  or  ```\n...\n```
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*\n?", "", cleaned).strip()
        cleaned = re.sub(r"\n```\s*$", "", cleaned).strip()
    # After step 1 removed leading text (which may have contained the
    # opening fence), the closing fence may remain at the end.
    cleaned = re.sub(r"\n```\s*$", "", cleaned)

    # 3. Parse front-matter so we can clean ONLY the body section
    #    without confusing front-matter closing --- with body-internal ---.
    try:
        post = _fm.loads(cleaned)
        meta = dict(post.metadata)
        body = post.content
    except Exception:
        # If front-matter parsing fails, treat everything as body
        meta = {}
        body = cleaned

    # 4. Strip trailing modification notes from the BODY only.
    #    The body may contain --- separators (promotion footers etc.).
    #    Only strip if the tail matches known agent-note keywords —
    #    do NOT use length heuristics which can strip legitimate content
    #    (e.g. short promotion footers after a --- separator).
    body = body.rstrip()
    if "\n---\n" in body:
        parts = body.rsplit("\n---\n", 1)
        if len(parts) == 2 and parts[1].strip():
            tail = parts[1].strip()
            if any(kw in tail.lower() for kw in
                   ["修改说明", "修改总结", "备注", "note:"]):
                body = parts[0]
    body = re.sub(
        r"\n(?:修改说明|修改总结|备注|Note)[：:].*$",
        "", body, flags=re.IGNORECASE)
    body = re.sub(r"\n---\s*$", "", body)
    # Strip orphaned code fence — step 1 may have absorbed the opening
    # fence as leading garbage; do this AFTER stripping notes so notes
    # between the fence end and the trailer are already removed.
    body = re.sub(r"\n```\s*$", "", body)
    body = body.strip()

    # 5. Detect if the body is agent thinking text rather than real article content.
    #    If so, fall back to the original body when available.  This prevents
    #    the agent's reasoning/summary from corrupting the draft.
    if _looks_like_thinking_text(body) and original_body is not None:
        logger.warning(
            "agent output body is thinking text (%d chars), falling back to original body",
            len(body),
        )
        body = original_body.strip()

    # 6. Reconstruct: front-matter (if any) + body
    result = _fm.dumps(_fm.Post(body, **meta))
    return result.strip() + "\n"


def create_app(config: Config | None = None,
               feeds_path: str | Path = "feeds.yaml") -> FastAPI:
    from app.log_setup import quiet_noisy_loggers
    quiet_noisy_loggers()  # silence trafilatura/courlan/urllib3 crawl noise
    config = config or Config.load()
    # Anchor every path used by request/background threads once at startup.
    # This is especially important for embedded/packaged callers that pass a
    # relative Config while the process cwd may later differ.
    config.data_dir = Path(config.data_dir).resolve()
    config.ensure_dirs()
    feeds_path = Path(feeds_path)

    # Apply DB overrides to the in-memory config so it reflects saved settings
    _init_conn = connect(config.db_path)
    init_db(_init_conn)
    _init_store = Store(_init_conn, config)
    config._apply_db_overrides(_init_store)
    _init_conn.close()

    # License manager (offline, machine-bound). Unactivated == free tier.
    license_mgr = LicenseManager(config.data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Recover stale running operations from a previous server session.
        # If the server crashed or was restarted while a pipeline was running,
        # those operations would be stuck as 'running' forever.  Mark them as
        # failed so the UI doesn't show phantom running tasks.
        conn = connect(config.db_path)
        init_db(conn)
        from app.db import recover_stale_ops
        recovered = recover_stale_ops(conn)
        if recovered:
            logger.info("Startup recovery: marked %d stale operation(s) as failed", recovered)
        conn.close()

        # 定时调度 is a Pro feature — only start the scheduler when licensed.
        if license_mgr.has_feature(LF.SCHEDULE):
            app.state.scheduler = start_if_enabled(get_store(), _scheduled_run)
        else:
            app.state.scheduler = None
        yield
        sched = getattr(app.state, "scheduler", None)
        if sched is not None and sched.running:
            sched.shutdown(wait=False)

    app = FastAPI(title="Media Agent", lifespan=lifespan)
    app.state.license = license_mgr          # same object the routes gate on
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    # ── Agent execution state (persists across page refreshes) ───────────
    # Keyed by draft_id.  Each entry:
    #   { "status": "running"|"completed"|"error"|"cancelled",
    #     "provider": CLIProvider instance (while running),
    #     "started_at": float (time.time()),
    #     "body_md": str (result, when completed),
    #     "error": str (when error),
    #     "prompt": str (original prompt) }
    _agent_tasks: dict[int, dict] = {}
    _agent_tasks_lock = threading.RLock()

    # ── React SPA serving (when frontend/dist is built) ──────────────────
    _spa_index = _SPA_DIST / "index.html"
    _spa_enabled = _spa_index.exists()
    if _spa_enabled and (_SPA_DIST / "assets").exists():
        app.mount("/assets", StaticFiles(directory=str(_SPA_DIST / "assets")),
                  name="spa-assets")

    def _serve_spa():
        """Return the SPA index.html when built, else a build prompt."""
        if _spa_enabled:
            return FileResponse(str(_spa_index))
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:2em'>"
            "<h1>前端未构建</h1><p>请运行：</p>"
            "<pre>cd frontend && npm install && npm run build</pre>"
            "<p>然后刷新此页面。</p></body></html>",
            status_code=200)

    def get_store() -> Store:
        conn = connect(config.db_path)
        init_db(conn)
        return Store(conn, config)

    def _current_config(store: Store | None = None) -> Config:
        """Reload DB settings without ever re-deriving this app's data_dir."""
        current = replace(config)
        current.data_dir = config.data_dir
        if store is not None:
            current._apply_db_overrides(store)
        return current

    def _db_conn() -> sqlite3.Connection:
        c = connect(config.db_path)
        init_db(c)
        return c

    def run_now(progress=None):
        store = get_store()
        # Reload config with DB overrides so settings changes take effect
        run_config = _current_config(store)
        feeds_cfg = load_feeds(feeds_path)
        provider = get_rewrite_provider(
            run_config.llm_provider, run_config.llm_api_key,
            run_config.llm_model, llm_api_base=run_config.llm_api_base,
            cli_tool=run_config.cli_tool, timeout=run_config.cli_timeout,
            llm_timeout=run_config.llm_timeout,
            priority=run_config.rewrite_priority)
        image_provider = get_image_provider(
            run_config.image_provider,
            run_config.image_api_key or run_config.llm_api_key,
            model=run_config.image_model,
            base_url=run_config.image_api_base or run_config.llm_api_base)
        def _rewrite_gate() -> bool:
            # Pro → unlimited; free → consume one daily-quota slot per rewrite.
            if not LG.rewrite_allowed(license_mgr, store):
                return False
            LG.consume_rewrite(license_mgr, store)
            return True

        style = rewrite_styles.resolve_style(run_config.rewrite_style, store)
        return run_pipeline(
            feeds_cfg, store, provider, image_provider=image_provider,
            record=True, max_drafts=run_config.max_drafts or 10,
            max_age_days=run_config.max_age_days,
            max_per_source=run_config.max_per_source,
            download_images=run_config.download_images,
            download_videos=run_config.download_videos,
            relevance_filter=run_config.relevance_filter,
            progress=progress,
            rewrite_gate=_rewrite_gate, style=style)

    # ---- background run state (for live progress / logs) ----
    run_lock = threading.Lock()
    run_state = {
        "running": False, "started_at": None, "finished_at": None,
        "stats": {}, "logs": [], "error": None,
        "paused": False, "stop_requested": False, "stopped": False,
        "source_current": 0, "source_total": 0,
    }
    search_create_lock = threading.Lock()
    search_create_state = {
        "running": False, "status": "idle", "stage": None,
        "detail": None, "current": 0, "total": 0, "stats": {},
        "logs": [], "error": None, "draft_id": None, "op_id": None,
        "started_at": None, "finished_at": None, "cancel_requested": False,
        "request": {},
    }
    _SOURCE_PROG_RE = re.compile(r"抓取来源 \[(\d+)/(\d+)\]")

    # ---- reachability check progress ----
    check_progress = {"running": False, "current": 0, "total": 0, "disabled": 0, "results": []}

    # ---- operations DB-backed registry (survives page refresh) ----
    from app.db import (
        start_op, finish_op, fail_op, update_op, cancel_op, get_latest_op,
        _op_log, get_running_ops)

    class _Ops:
        """Thin wrapper that mirrors log lines to a DB operations record so
        state survives page refreshes and server restarts."""

        def __init__(self):
            self._active: dict[str, int] = {}  # key → op_id

        def begin(self, key: str, op_type: str, target_id: int = 0,
                  conn: sqlite3.Connection | None = None) -> int | None:
            if conn is None:
                return None
            op_id = start_op(conn, op_type, target_id)
            self._active[key] = op_id
            return op_id

        def log(self, key: str, line: str,
                conn: sqlite3.Connection | None = None) -> None:
            if conn is None:
                return
            op_id = self._active.get(key)
            if op_id is not None:
                _op_log(conn, op_id, line)
                conn.commit()

        def done(self, key: str, stats: dict | None = None,
                 conn: sqlite3.Connection | None = None) -> None:
            if conn is None:
                return
            op_id = self._active.pop(key, None)
            if op_id is not None:
                finish_op(conn, op_id, stats)

        def error(self, key: str, exc: str,
                  conn: sqlite3.Connection | None = None) -> None:
            if conn is None:
                return
            op_id = self._active.pop(key, None)
            if op_id is not None:
                fail_op(conn, op_id, exc)

    ops = _Ops()

    def _log(msg, stats=None):
        line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
        with run_lock:
            run_state["logs"].append(line)
            # keep only the most recent lines to bound memory
            if len(run_state["logs"]) > 500:
                del run_state["logs"][:-500]
            if stats is not None:
                run_state["stats"] = dict(stats)
            # Parse source progress from "抓取来源 [n/m]：..."
            m = _SOURCE_PROG_RE.search(line)
            if m:
                run_state["source_current"] = int(m.group(1))
                run_state["source_total"] = int(m.group(2))

    def _pausable_log(msg, stats=None):
        """Like _log but blocks while paused / raises when stop requested."""
        while True:
            with run_lock:
                if run_state["stop_requested"]:
                    run_state["stopped"] = True
                    raise SystemExit("stopped")
                if not run_state["paused"]:
                    break
            time.sleep(0.3)
        _log(msg, stats)

    def _scheduled_run():
        """Scheduler callback — mirrors trigger_run so scheduled runs
        show real-time progress and disable the Run Now button."""
        with run_lock:
            if run_state["running"]:
                return  # already running, skip this cycle
            run_state.update(running=True, error=None, stats={}, logs=[],
                             started_at=datetime.now().isoformat(timespec="seconds"),
                             finished_at=None, paused=False,
                             stop_requested=False, stopped=False)
        try:
            _log("开始定时运行流水线")
            stats = run_now(progress=_pausable_log)
            with run_lock:
                if stats:
                    run_state["stats"] = dict(stats)
            _log("定时运行成功结束", run_state["stats"])
        except Exception as e:  # noqa: BLE001 - surface to UI log
            with run_lock:
                run_state["error"] = str(e)
            _log(f"定时运行出错：{e}")
        finally:
            with run_lock:
                run_state["running"] = False
                run_state["finished_at"] = datetime.now().isoformat(
                    timespec="seconds")

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        return _serve_spa()

    @app.get("/api/stats")
    def api_stats():
        return get_store().dashboard_stats()

    @app.get("/api/dashboard")
    def api_dashboard():
        """All dashboard data for the SPA in one call."""
        store = get_store()
        runs = [dict(r) for r in store.list_runs(limit=10)]
        drafts = [dict(d) for d in store.list_drafts()[:10]]
        raw_hot = store.get_setting("hotness_data")
        hotness = json.loads(raw_hot) if raw_hot else None
        return {
            "stats": store.dashboard_stats(),
            "runs": [{"started_at": r.get("started_at"),
                      "status": r.get("status"),
                      "stats_json": r.get("stats_json")} for r in runs],
            "drafts": [{"id": d.get("id"), "status": d.get("status"),
                        "draft_path": d.get("draft_path")} for d in drafts],
            "hotness": hotness,
            "hotness_updated": store.get_setting("hotness_updated_at"),
        }

    @app.post("/api/refresh-hotness")
    def api_refresh_hotness():
        store = get_store()
        cfg = load_feeds(feeds_path) if feeds_path.exists() else None
        try:
            data = compute_hotness(store, feeds_cfg=cfg,
                                   provider=_resolve_provider())
        except Exception:
            data = compute_hotness(store)
        store.set_setting("hotness_data", json.dumps(data))
        store.set_setting("hotness_updated_at",
                          datetime.now(timezone.utc).isoformat())
        return {"status": "ok", **data}

    @app.get("/archive", response_class=HTMLResponse)
    def archive():
        return _serve_spa()

    @app.get("/api/archive")
    def api_archive(topic: str | None = None, source: str | None = None):
        from collections import OrderedDict
        store = get_store()
        articles = store.list_articles(limit=200, topic=topic, source=source)
        video_flags = store.article_video_flags(articles)
        by_date: dict[str, list] = OrderedDict()
        for a in articles:
            dt = a["published_at"] or a["fetched_at"]
            date_key = dt[:10] if dt else "未知日期"
            by_date.setdefault(date_key, []).append(a)
        groups = []
        for date_key in sorted(by_date, reverse=True):
            day = sorted(by_date[date_key], key=lambda a: a["source_name"] or "")
            groups.append({"date": date_key, "articles": [{
                "id": a["id"], "title": a["title"], "url": a["url"],
                "topic": a["topic"], "source_name": a["source_name"],
                "published_at": a["published_at"], "fetched_at": a["fetched_at"],
                "has_video": video_flags.get(a["id"], False),
            } for a in day]})
        return {
            "groups": groups,
            "topics": store.list_topics(),
            "draft_map": {str(k): v for k, v in store.drafts_by_article().items()},
        }

    @app.get("/api/archive/{article_id}/view")
    def api_archive_view(article_id: int):
        store = get_store()
        row = store.get_article(article_id)
        if not row:
            return JSONResponse({"ok": False, "error": "文章不存在"}, status_code=404)
        body = store.read_article_body(article_id)
        return {
            "ok": True,
            "article": {
                "title": row["title"], "url": row["url"],
                "source_name": row["source_name"], "topic": row["topic"],
                "published_at": row["published_at"], "fetched_at": row["fetched_at"],
            },
            "content_md": body.get("content_md", ""),
            "images": body.get("images", []) or [],
            "videos": body.get("videos", []) or [],
        }

    @app.get("/archive/{article_id}/view", response_class=HTMLResponse)
    def archive_view(article_id: int):
        store = get_store()
        row = store.get_article(article_id)
        if not row:
            return HTMLResponse("文章不存在", status_code=404)
        return _serve_spa()

    @app.post("/api/archive/delete")
    async def api_archive_delete(request: Request):
        """Batch-delete archived articles (and their dependent drafts + files)."""
        data = await request.json()
        raw_ids = data.get("ids") or []
        ids: list[int] = []
        for x in raw_ids:
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                continue
        if not ids:
            return JSONResponse({"ok": False, "error": "未选择文章"}, status_code=400)
        deleted = get_store().delete_articles(ids)
        return {"ok": True, "deleted": deleted}

    @app.post("/api/drafts/delete")
    async def api_drafts_delete(request: Request):
        """Batch-delete drafts (DB rows + their markdown files)."""
        data = await request.json()
        raw_ids = data.get("ids") or []
        ids: list[int] = []
        for x in raw_ids:
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                continue
        if not ids:
            return JSONResponse({"ok": False, "error": "未选择草稿"}, status_code=400)
        deleted = get_store().delete_drafts(ids)
        return {"ok": True, "deleted": deleted}

    @app.post("/archive/{article_id}/refetch")
    def archive_refetch(article_id: int):
        from app.sources.scraper import scrape_single
        from app.pipeline.localize import localize_article
        store = get_store()
        row = store.get_article(article_id)
        if not row:
            return {"ok": False, "error": "文章不存在"}
        try:
            art = scrape_single(row["url"], row["source_name"] or "",
                                proxy=store.config.fetch_proxy)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"抓取失败：{e}"}
        if not art or not art.content_md:
            return {"ok": False, "error": "未抓到正文（可能需要登录/JS 渲染）"}
        # Auto-localize media after refetch
        localized = 0
        try:
            localize_article(art, store.config,
                             download_images=store.config.download_images,
                             download_videos=store.config.download_videos)
            localized = sum(1 for u in art.images + art.videos if u.startswith("/media/"))
        except Exception:                          # noqa: BLE001
            pass
        store.update_article_archive(article_id, art.content_md, art.images,
                                     art.videos)
        return {"ok": True, "images": len(art.images),
                "videos": len(art.videos), "localized": localized}

    @app.post("/archive/{article_id}/rewrite")
    def archive_rewrite(article_id: int, style: str = Form("")):
        store = get_store()
        row = store.get_article(article_id)
        if not row:
            return {"ok": False, "error": "文章不存在"}
        if not LG.rewrite_allowed(license_mgr, store):
            return LG.rewrite_error()
        # Resolve the requested style (empty → global default). Capture the
        # concrete style now so the worker thread uses a consistent value.
        chosen_style = rewrite_styles.resolve_style(style.strip() or None, store)
        body = store.read_article_body(article_id)
        content_md = body.get("content_md", "")
        images = body.get("images") or _images_from_html(content_md)
        videos = body.get("videos") or _videos_from_html(
            content_md, base_url=row["url"])

        art = Article(
            title=row["title"], content_md=content_md, url=row["url"],
            source_name=row["source_name"], source_type=row["source_type"],
            published_at=None, images=images or [], raw_summary=None,
            fetched_at=datetime.now(tz=timezone.utc), topic=row["topic"],
            archive_path=row["archive_path"], id=row["id"], videos=videos)

        st = _rewrite_state(article_id)
        lk = rewrite_locks[article_id]
        with lk:
            if st["running"]:
                return {"started": False, "running": True,
                        "message": "该文章已有转写任务在进行"}
            st.update(running=True, logs=[], error=None, done=False,
                      draft_id=None)
            LG.consume_rewrite(license_mgr, store)   # free-tier daily quota

        def worker():
            op_key = f"rewrite-{article_id}"
            op_conn = _db_conn()
            ops.begin(op_key, "rewrite", article_id, op_conn)
            try:
                _rewrite_log(article_id, "构建 LLM provider…",
                             op_key=op_key, op_conn=op_conn)
                ws = get_store()
                run_config = _current_config(ws)
                provider = get_rewrite_provider(
                    run_config.llm_provider, run_config.llm_api_key,
                    run_config.llm_model, llm_api_base=run_config.llm_api_base,
                    cli_tool=run_config.cli_tool,
                    timeout=run_config.cli_timeout,
                    llm_timeout=run_config.llm_timeout,
                    priority=run_config.rewrite_priority)
                image_provider = get_image_provider(
                    run_config.image_provider,
                    run_config.image_api_key or run_config.llm_api_key,
                    model=run_config.image_model,
                    base_url=run_config.image_api_base
                           or run_config.llm_api_base)

                _rewrite_log(article_id, f"正在调用 LLM 进行转写…（风格：{chosen_style.name}）",
                             op_key=op_key, op_conn=op_conn)
                draft = rewrite(art, provider, style=chosen_style,
                                promotion_footer=run_config.promotion_footer,
                                seo_tags_enabled=run_config.seo_tags_enabled)
                _rewrite_log(article_id, "转写完成，进行敏感词过滤…",
                             op_key=op_key, op_conn=op_conn)
                sanitize_draft(draft, load_words(run_config))

                _rewrite_log(article_id, "保存草稿…",
                             op_key=op_key, op_conn=op_conn)
                old = ws.get_draft_for_article(article_id)
                if old:
                    draft.id = old["id"]
                saved = ws.save_draft(draft)

                # Compute and store draft score
                try:
                    score_val = compute_draft_score(
                        title=draft.title_candidates[0] if draft.title_candidates else "",
                        topic=draft.topic,
                        source_name=draft.source_name,
                        body_preview=draft.body_md,
                        published_at=row["published_at"] if row else None,
                        provider=provider,
                    )
                    ws.update_draft_score(saved.id, score_val)
                    _rewrite_log(article_id, f"评分：{score_val}/100",
                                 op_key=op_key, op_conn=op_conn)
                except Exception as exc:
                    _rewrite_log(article_id, f"评分失败（已跳过）：{exc}",
                                 op_key=op_key, op_conn=op_conn)

                if image_provider is not None:
                    _rewrite_log(article_id, "生成封面…",
                                 op_key=op_key, op_conn=op_conn)
                    attach_cover(saved, image_provider,
                                 ws.config.images_dir)
                    if saved.cover_image:
                        ws.set_draft_cover(saved.id, saved.cover_image)
                    else:
                        _append_prompt(saved, ws)
                else:
                    _append_prompt(saved, ws)

                with rewrite_locks[article_id]:
                    st["draft_id"] = saved.id
                _rewrite_log(article_id, f"转写完成 ✓ 草稿 #{saved.id}",
                             op_key=op_key, op_conn=op_conn)
                ops.done(op_key, {"draft_id": saved.id}, op_conn)
            except Exception as e:  # noqa: BLE001
                _rewrite_log(article_id, f"出错：{e}",
                             op_key=op_key, op_conn=op_conn)
                with rewrite_locks[article_id]:
                    st["error"] = str(e)
                ops.error(op_key, str(e), op_conn)
            finally:
                with rewrite_locks[article_id]:
                    st["running"] = False
                    st["done"] = True

        threading.Thread(target=worker, daemon=True).start()
        return {"started": True, "running": True}

    @app.get("/api/operations")
    def api_operations():
        """Return all currently running operations, so the frontend can
        resume tracking after a page refresh."""
        conn = _db_conn()
        running = get_running_ops(conn)
        conn.close()
        return {"running": running}

    @app.get("/api/rewrite-status")
    def api_rewrite_status(article_id: int):
        st = _rewrite_state(article_id)
        with rewrite_locks[article_id]:
            return {
                "running": st["running"],
                "done": st["done"],
                "error": st["error"],
                "draft_id": st["draft_id"],
                "logs": list(st["logs"]),
            }

    @app.get("/api/rewrite-all-status")
    def api_rewrite_all_status():
        """Return status for ALL currently-tracked rewrites with article titles."""
        store = get_store()
        results = []
        with rewrite_main_lock:
            ids = list(rewrite_states.keys())
        for aid in ids:
            st = _rewrite_state(aid)
            with rewrite_locks[aid]:
                if not st["running"] and not st["done"]:
                    continue  # skip freshly initialized, no-op entries
                title = ""
                try:
                    row = store.get_article(aid)
                    if row:
                        title = row["title"]
                except Exception:
                    pass
                results.append({
                    "article_id": aid,
                    "title": title,
                    "running": st["running"],
                    "done": st["done"],
                    "error": st["error"],
                    "draft_id": st["draft_id"],
                    "logs": list(st["logs"]),
                })
        return {"entries": results}

    # ── Rewrite styles (转写风格) CRUD ──────────────────────────────────
    @app.get("/api/rewrite-styles")
    def api_list_rewrite_styles():
        store = get_store()
        default_id = rewrite_styles.get_default_style_id(store)
        return {
            "default": default_id,
            "styles": [s.to_public() for s in rewrite_styles.all_styles(store)],
        }

    @app.post("/api/rewrite-styles")
    def api_create_rewrite_style(name: str = Form(...),
                                 description: str = Form(""),
                                 prompt: str = Form(""),
                                 instruction: str = Form(...)):
        store = get_store()
        try:
            s = rewrite_styles.save_custom_style(
                store, id=None, name=name, description=description,
                prompt=prompt, instruction=instruction)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return {"ok": True, "style": s.to_public()}

    @app.put("/api/rewrite-styles/{style_id}")
    def api_update_rewrite_style(style_id: str, name: str = Form(...),
                                 description: str = Form(""),
                                 prompt: str = Form(""),
                                 instruction: str = Form(...)):
        store = get_store()
        try:
            s = rewrite_styles.save_custom_style(
                store, id=style_id, name=name, description=description,
                prompt=prompt, instruction=instruction)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return {"ok": True, "style": s.to_public()}

    @app.delete("/api/rewrite-styles/{style_id}")
    def api_delete_rewrite_style(style_id: str):
        store = get_store()
        try:
            ok = rewrite_styles.delete_custom_style(store, style_id)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        if not ok:
            return JSONResponse({"ok": False, "error": "风格不存在"}, status_code=404)
        return {"ok": True}

    # ── 秀米-style editor: components / templates / materials (issue #31) ──
    @app.get("/api/editor/components")
    def api_editor_components(category: str | None = None):
        store = get_store()
        comps = editor_components.all_components(store)
        if category:
            comps = [c for c in comps if c.category == category]
        return {
            "categories": editor_components.CATEGORIES,
            "components": [c.to_public() for c in comps],
        }

    @app.post("/api/editor/components")
    def api_editor_component_create(name: str = Form(...),
                                    markdown: str = Form(...),
                                    category: str = Form("自定义"),
                                    tags: str = Form("")):
        store = get_store()
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        try:
            c = editor_components.save_custom_component(
                store, id=None, name=name, category=category,
                markdown=markdown, tags=tag_list)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return {"ok": True, "component": c.to_public()}

    @app.put("/api/editor/components/{comp_id}")
    def api_editor_component_update(comp_id: str, name: str = Form(...),
                                    markdown: str = Form(...),
                                    category: str = Form("自定义"),
                                    tags: str = Form("")):
        store = get_store()
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        try:
            c = editor_components.save_custom_component(
                store, id=comp_id, name=name, category=category,
                markdown=markdown, tags=tag_list)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return {"ok": True, "component": c.to_public()}

    @app.delete("/api/editor/components/{comp_id}")
    def api_editor_component_delete(comp_id: str):
        store = get_store()
        try:
            ok = editor_components.delete_custom_component(store, comp_id)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        if not ok:
            return JSONResponse({"ok": False, "error": "组件不存在"}, status_code=404)
        return {"ok": True}

    @app.get("/api/editor/templates")
    def api_editor_templates():
        store = get_store()
        return {"templates": [t.to_public()
                              for t in editor_components.all_templates(store)],
                "categories": editor_components.TEMPLATE_CATEGORIES}

    @app.post("/api/editor/templates")
    def api_editor_template_create(name: str = Form(...),
                                   markdown: str = Form(...),
                                   theme: str = Form("default")):
        store = get_store()
        try:
            t = editor_components.save_custom_template(
                store, id=None, name=name, markdown=markdown, theme=theme)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return {"ok": True, "template": t.to_public()}

    @app.put("/api/editor/templates/{template_id}")
    def api_editor_template_update(template_id: str, name: str = Form(...),
                                   markdown: str = Form(...),
                                   theme: str = Form("default")):
        store = get_store()
        try:
            t = editor_components.save_custom_template(
                store, id=template_id, name=name, markdown=markdown, theme=theme)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return {"ok": True, "template": t.to_public()}

    @app.delete("/api/editor/templates/{template_id}")
    def api_editor_template_delete(template_id: str):
        store = get_store()
        try:
            ok = editor_components.delete_custom_template(store, template_id)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        if not ok:
            return JSONResponse({"ok": False, "error": "模板不存在"},
                                status_code=404)
        return {"ok": True}

    @app.post("/api/editor/templates/{template_id}/apply")
    def api_editor_template_apply(template_id: str):
        """Return the template's markdown outline so the client can insert it
        into the editor (non-destructive — the user saves explicitly)."""
        tpl = editor_components.get_template(template_id, get_store())
        if not tpl:
            return JSONResponse({"ok": False, "error": "模板不存在"},
                                status_code=404)
        return {"ok": True, "markdown": tpl.markdown, "theme": tpl.theme,
                "name": tpl.name}

    @app.get("/api/editor/materials")
    def api_editor_materials():
        return {"materials": [m.to_public()
                             for m in editor_components.materials()]}

    @app.get("/drafts", response_class=HTMLResponse)
    def drafts_list():
        return _serve_spa()

    @app.get("/api/drafts")
    def api_drafts(status: str | None = None, limit: int = 30, offset: int = 0):
        from datetime import datetime, timezone, timedelta
        CST = timezone(timedelta(hours=8))
        store = get_store()
        # Server-side pagination: only the requested page is read from disk
        # (each untitled draft costs a file read for its display title), so the
        # 草稿 page loads fast even with a very long list.
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        total = store.count_drafts(status=status)
        drafts = store.list_drafts(status=status, limit=limit, offset=offset)
        out = []
        for d in drafts:
            item = dict(d)
            if not item.get("title_cn"):
                body = store.read_draft_body(item["id"])
                candidates = body.get("title_candidates", []) or []
                item["display_title"] = candidates[0] if candidates else None
            else:
                item["display_title"] = None
            item["draft_filename"] = os.path.basename(item.get("draft_path", ""))
            raw = item.get("updated_at", "")
            if raw:
                try:
                    dt = datetime.fromisoformat(str(raw))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    item["updated_at"] = dt.astimezone(CST).strftime("%Y-%m-%d %H:%M")
                except Exception:  # noqa: BLE001
                    pass
            out.append({
                "id": item.get("id"), "status": item.get("status"),
                "title_cn": item.get("title_cn"),
                "display_title": item.get("display_title"),
                "score": item.get("score"),
                "origin": item.get("origin") or "rewrite",
                "draft_filename": item.get("draft_filename"),
                "draft_path": item.get("draft_path"),
                "article_published_at": item.get("article_published_at"),
                "updated_at": item.get("updated_at"),
            })
        return {"drafts": out, "total": total,
                "statuses": ["drafted", "reviewing", "approved", "published"]}

    @app.get("/api/draft/{draft_id}")
    def api_draft(draft_id: int):
        from app.wechat.formatter import list_themes as _list_themes
        store = get_store()
        row = store.get_draft(draft_id)
        if not row:
            return JSONResponse({"ok": False, "error": "草稿不存在"}, status_code=404)
        body = store.read_draft_body(draft_id)
        title_candidates = body.get("title_candidates", []) or []
        article = store.get_article(row["article_id"]) if row["article_id"] else None
        return {
            "ok": True,
            "id": draft_id,
            "status": row["status"],
            "title_cn": body.get("title_cn") or (title_candidates[0] if title_candidates else ""),
            "title_candidates": title_candidates,
            "body_md": body.get("body_md", ""),
            "cover_image": body.get("cover_image"),
            "source_url": body.get("source_url", ""),
            "source_name": body.get("source_name", ""),
            "flagged_claims": body.get("flagged_claims", []) or [],
            "sensitive_hits": body.get("sensitive_hits", []) or [],
            "origin": body.get("origin") or row["origin"] or "rewrite",
            "sources": body.get("sources", []) or [],
            "citations": body.get("citations", []) or [],
            "search_meta": body.get("search_meta", {}) or {},
            "article_id": row["article_id"],
            "article_published_at": article["published_at"] if article else None,
            "article_title": title_candidates[0] if title_candidates else "",
            "has_video": video_path(draft_id, config) is not None,
            "has_narration": bool(load_narration(draft_id, config)),
            "video_brand_name": config.video_brand_name or "Media Agent",
            "statuses": ["drafted", "reviewing", "approved", "published"],
            "platforms": [{"id": p.id, "label": p.label,
                           "publish_url": getattr(p, "publish_url", None),
                           "video_publish_url": getattr(p, "video_publish_url", None)}
                          for p in list_platforms()],
            "wechat_themes": _list_themes("wechat"),
        }

    @app.get("/drafts/{draft_id}/edit", response_class=HTMLResponse)
    def draft_edit(draft_id: int):
        return _serve_spa()

    @app.post("/drafts/{draft_id}")
    def draft_save(draft_id: int, title_candidates: str = Form(""),
                   body_md: str = Form(""), status: str = Form("drafted"),
                   title_cn: str = Form(""), from_page: str = Form("")):
        store = get_store()
        titles = [t.strip() for t in title_candidates.splitlines() if t.strip()]
        store.update_draft_body(draft_id, titles, body_md, status=status,
                                title_cn=title_cn.strip() or None)
        redirect_url = f"/drafts/{draft_id}/edit"
        if from_page in ("archive", "drafts"):
            redirect_url += f"?from={from_page}"
        return RedirectResponse(url=redirect_url, status_code=303)

    # ── Agent edit (draft → CLI agent) ──────────────────────────────────────

    def _agent_workspace(draft_id: int, run_id: str) -> Path:
        """Return an isolated, data-dir-scoped workspace for exactly one run."""
        # Remove the legacy shared scratch file from releases that wrote
        # data/output-draft.md. It is never consumed by the new workflow.
        try:
            (config.data_dir / "output-draft.md").unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove legacy Agent output: %s", exc)
        root = (config.data_dir / "agent-workspaces").resolve()
        workspace = (root / f"draft-{draft_id}" / run_id).resolve()
        if root != workspace and root not in workspace.parents:
            raise RuntimeError("Agent 工作目录解析到了数据目录之外")
        workspace.mkdir(parents=True, exist_ok=False)
        return workspace

    def _agent_task_is_current(draft_id: int, run_id: str,
                               *, running: bool = False) -> bool:
        task = _agent_tasks.get(draft_id)
        return bool(
            task and task.get("run_id") == run_id
            and (not running or task.get("status") == "running")
        )

    def _set_agent_error(draft_id: int, run_id: str, error: str) -> None:
        with _agent_tasks_lock:
            if not _agent_task_is_current(draft_id, run_id, running=True):
                return
            task = _agent_tasks[draft_id]
            task["status"] = "error"
            task["error"] = error
            task.pop("provider", None)

    def _agent_run_background(draft_id: int, run_id: str, prompt: str,
                              full_md: str, provider: LLMProvider,
                              workspace: Path):
        """Run one isolated Agent edit and publish only its current result."""
        out_path = workspace / "output-draft.md"
        try:
            messages = [
                Message(role="system", content=(
                    "你是一个文章编辑助手。用户会给你一篇完整草稿文件（包含 front-matter 元信息和正文 markdown）"
                    "以及修改指令。\n\n"
                    "规则：\n"
                    "1. 只修改正文；保留 front-matter 元信息和标题。\n"
                    "2. 完整输出修改后的整篇文件（包含 front-matter）。\n"
                    "3. 禁止输出修改说明、总结、备注、注释、思考过程、TODO 或代码块。\n"
                    "4. 用户如果要求插入图片，用标准 Markdown 图片语法 `![描述](图片URL)`。\n"
                    f"5. 如果具备文件写入能力，必须把最终完整文件写入这个绝对路径："
                    f"`{out_path}`（UTF-8）；当前工作目录也是该文件所在目录。"
                    "不要在项目目录或其他目录创建 output-draft.md。"
                    "如果不能写文件，就把完整文件输出到 stdout。")),
                Message(role="user", content=(
                    f"## 当前草稿文件\n```markdown\n{full_md}\n```\n\n"
                    f"## 用户指令\n{prompt}")),
            ]

            # Agentic CLIs are allowed to return empty stdout when their actual
            # result was written to the isolated output file.
            if isinstance(provider, cli_provider.CLIProvider):
                stdout_result = provider.chat(messages, allow_empty_output=True)
            else:
                stdout_result = provider.chat(messages)

            with _agent_tasks_lock:
                if not _agent_task_is_current(draft_id, run_id, running=True):
                    return

            file_result = ""
            if out_path.is_file():
                try:
                    file_result = out_path.read_text(encoding="utf-8").strip()
                except (OSError, UnicodeError) as exc:
                    raise RuntimeError(
                        f"Agent 输出文件无法读取（{out_path}）：{exc}") from exc
            raw_result = file_result or (stdout_result or "").strip()
            if not raw_result:
                raise RuntimeError(
                    "Agent 没有生成可应用的正文：stdout 为空，且未创建 "
                    f"{out_path}")

            result = _clean_agent_output(raw_result)
            import frontmatter as _fm
            parsed = _fm.loads(result)
            parsed_body = parsed.content.strip()
            if not parsed_body:
                raise RuntimeError("Agent 输出格式无效：解析后的正文为空")
            # A short body is valid when the CLI returned the required
            # front-matter. Without metadata, short/no-heading output is much
            # more likely to be a CLI execution summary than the full draft.
            if _looks_like_thinking_text(parsed_body) and not parsed.metadata:
                source = "输出文件" if file_result else "stdout"
                raise RuntimeError(
                    f"Agent {source} 看起来是执行摘要而不是完整正文；"
                    f"请检查 CLI 权限，或确认它写入了 {out_path}")

            # Publish the result to task state only. The explicit apply endpoint
            # performs the draft write, so closing a completed dialog does not
            # silently overwrite the editor or a later manual save.
            with _agent_tasks_lock:
                if not _agent_task_is_current(draft_id, run_id, running=True):
                    return
                task = _agent_tasks[draft_id]
                task.update({
                    "status": "completed",
                    "body_md": parsed_body,
                    "title_candidates": task["title_candidates"],
                    "title_cn": task["title_cn"],
                })
                task.pop("provider", None)
                logger.info("agent-edit draft=%d run=%s result ready",
                            draft_id, run_id)

        except RuntimeError as exc:
            logger.error("agent-edit draft=%d run=%s failed: %s",
                         draft_id, run_id, exc)
            _set_agent_error(draft_id, run_id, str(exc))
        except Exception as exc:
            logger.exception("agent-edit draft=%d run=%s unexpected error",
                             draft_id, run_id)
            _set_agent_error(draft_id, run_id, f"内部错误: {exc}")
        finally:
            # This path was created by _agent_workspace and is unique to this
            # run, so recursive cleanup cannot touch another draft or user file.
            shutil.rmtree(workspace, ignore_errors=True)

    @app.post("/api/draft/{draft_id}/agent-edit")
    def draft_agent_edit(draft_id: int, prompt: str = Form(...),
                         body_md: str | None = Form(None),
                         title_cn: str | None = Form(None),
                         title_candidates: str | None = Form(None)):
        """Start CLI agent execution in background. Returns immediately."""
        store = get_store()

        if not prompt.strip():
            return JSONResponse(
                {"ok": False, "error": "修改指令不能为空"}, status_code=400)

        # 1. get draft content
        body = store.read_draft_body(draft_id)
        if not body or "body_md" not in body:
            return JSONResponse({"ok": False, "error": "草稿不存在或内容为空"}, status_code=404)

        row = store.get_draft(draft_id)
        if not row:
            return JSONResponse(
                {"ok": False, "error": "草稿不存在"}, status_code=404)
        source_body = body["body_md"] if body_md is None else body_md
        source_titles = body.get("title_candidates", []) or []
        if title_candidates is not None:
            source_titles = [
                item.strip() for item in title_candidates.splitlines()
                if item.strip()
            ]
        source_title_cn = (
            (body.get("title_cn") or row["title_cn"] or "")
            if title_cn is None else title_cn.strip()
        )
        import frontmatter
        source_meta = {k: v for k, v in body.items() if k != "body_md"}
        source_meta["title_candidates"] = source_titles
        if source_title_cn:
            source_meta["title_cn"] = source_title_cn
        else:
            source_meta.pop("title_cn", None)
        full_md = frontmatter.dumps(
            frontmatter.Post(source_body, **source_meta))

        # 2. determine active CLI tool
        rc = _current_config(store)
        active_tool = rc.cli_tool or "auto"
        if active_tool in ("none", "", None):
            return JSONResponse(
                {"ok": False, "error": "未配置 CLI Agent，请在设置页选择并保存后再试"},
                status_code=400)

        if active_tool == "auto":
            active_tool = cli_provider.detect_all()
            if not active_tool:
                return JSONResponse(
                    {"ok": False, "error": "自动检测未找到已安装的 CLI Agent，请先在设置页配置"},
                    status_code=400)

        # 3. verify the tool is actually installed
        if not cli_provider.detect(active_tool):
            return JSONResponse(
                {"ok": False, "error": f"Agent '{active_tool}' 未安装或不在 PATH 中"},
                status_code=400)

        try:
            run_id = uuid.uuid4().hex
            workspace = _agent_workspace(draft_id, run_id)
        except OSError as exc:
            return JSONResponse(
                {"ok": False,
                 "error": f"无法创建 Agent 工作目录（{config.data_dir}）：{exc}"},
                status_code=500)
        provider = cli_provider.CLIProvider(
            active_tool, timeout=rc.cli_timeout, model=rc.llm_model or "",
            cwd=workspace,
        )
        with _agent_tasks_lock:
            if (_agent_tasks.get(draft_id, {}).get("status") == "running"):
                shutil.rmtree(workspace, ignore_errors=True)
                return JSONResponse(
                    {"ok": False,
                     "error": "Agent 正在处理中，请等待完成或取消当前任务"},
                    status_code=409)
            _agent_tasks[draft_id] = {
                "status": "running",
                "run_id": run_id,
                "provider": provider,
                "started_at": time.time(),
                "prompt": prompt,
                "workspace": str(workspace),
                "title_candidates": source_titles,
                "title_cn": source_title_cn,
                "draft_status": row["status"],
            }

        t = threading.Thread(
            target=_agent_run_background,
            args=(draft_id, run_id, prompt, full_md, provider, workspace),
            daemon=True,
        )
        t.start()

        return {"ok": True, "running": True, "draft_id": draft_id,
                "run_id": run_id}

    @app.post("/api/draft/{draft_id}/apply-template")
    def draft_apply_template(draft_id: int, template_id: str = Form(...),
                             body_md: str | None = Form(None),
                             title_cn: str | None = Form(None),
                             title_candidates: str | None = Form(None)):
        """Rewrite the draft so it follows the selected template's structure and
        visual style. Honors the configured rewrite priority (LLM vs CLI Agent)
        and reuses the agent background flow + /agent-status polling."""
        store = get_store()

        with _agent_tasks_lock:
            if _agent_tasks.get(draft_id, {}).get("status") == "running":
                return JSONResponse(
                    {"ok": False, "error": "正在处理中，请等待完成或取消当前任务"},
                    status_code=409)

        # Template rewrite is an LLM/Agent rewrite → same quota as other rewrites
        # (free: FREE_REWRITE_PER_DAY/day shared with archive rewrite; Pro: 无限).
        if not LG.rewrite_allowed(license_mgr, store):
            return LG.rewrite_error()

        body = store.read_draft_body(draft_id)
        if not body or "body_md" not in body:
            return JSONResponse({"ok": False, "error": "草稿不存在或内容为空"},
                                status_code=404)

        tpl = editor_components.get_template(template_id, store)
        if not tpl:
            return JSONResponse({"ok": False, "error": "模板不存在"},
                                status_code=404)

        row = store.get_draft(draft_id)
        source_body = body["body_md"] if body_md is None else body_md
        source_titles = body.get("title_candidates", []) or []
        if title_candidates is not None:
            source_titles = [
                item.strip() for item in title_candidates.splitlines()
                if item.strip()
            ]
        source_title_cn = (
            (body.get("title_cn") or row["title_cn"] or "")
            if title_cn is None else title_cn.strip()
        )
        import frontmatter
        source_meta = {k: v for k, v in body.items() if k != "body_md"}
        source_meta["title_candidates"] = source_titles
        if source_title_cn:
            source_meta["title_cn"] = source_title_cn
        else:
            source_meta.pop("title_cn", None)
        full_md = frontmatter.dumps(
            frontmatter.Post(source_body, **source_meta))

        # Resolve provider honoring rewrite_priority (LLM or CLI Agent).
        rc = _current_config(store)
        provider = _resolve_provider(rc)
        run_id = uuid.uuid4().hex
        try:
            workspace = _agent_workspace(draft_id, run_id)
        except OSError as exc:
            return JSONResponse(
                {"ok": False,
                 "error": f"无法创建 Agent 工作目录（{config.data_dir}）：{exc}"},
                status_code=500)
        if isinstance(provider, cli_provider.CLIProvider):
            provider._cwd = str(workspace)

        prompt = (
            "请把这篇文章按照下面「目标模板」的结构、排版层级和视觉组件进行重写：\n"
            "- 保留文章的事实、数据与核心信息，不要编造内容；\n"
            "- 套用模板的标题层级、分段方式与视觉组件（如 :::tip 提示卡片、"
            ":::center 居中、==高亮==、{color:#HEX} 彩色字、::::columns 双栏 等），"
            "并保留模板的结尾结构；\n"
            "- 用模板的板式重新组织正文，使成品排版风格与模板一致；\n"
            "- 保留 front-matter 中的 title_candidates 等元信息（可按需润色标题）。\n\n"
            f"## 目标模板（{tpl.name}）\n```markdown\n{tpl.markdown}\n```"
        )

        with _agent_tasks_lock:
            if _agent_tasks.get(draft_id, {}).get("status") == "running":
                shutil.rmtree(workspace, ignore_errors=True)
                return JSONResponse(
                    {"ok": False, "error": "正在处理中，请等待完成或取消当前任务"},
                    status_code=409)
            _agent_tasks[draft_id] = {
                "status": "running",
                "run_id": run_id,
                "provider": provider,
                "started_at": time.time(),
                "prompt": prompt,
                "workspace": str(workspace),
                "title_candidates": source_titles,
                "title_cn": source_title_cn,
                "draft_status": row["status"],
            }
        LG.consume_rewrite(license_mgr, store)   # free-tier daily quota
        t = threading.Thread(
            target=_agent_run_background,
            args=(draft_id, run_id, prompt, full_md, provider, workspace),
            daemon=True,
        )
        t.start()

        return {"ok": True, "running": True, "draft_id": draft_id,
                "run_id": run_id,
                "template": tpl.name}

    @app.get("/api/draft/{draft_id}/agent-status")
    def draft_agent_status(draft_id: int):
        """Poll agent execution status."""
        with _agent_tasks_lock:
            task = _agent_tasks.get(draft_id)
            if not task:
                return {"ok": True, "status": "idle"}

            elapsed = time.time() - task["started_at"]
            base = {
                "ok": True,
                "status": task["status"],
                "run_id": task.get("run_id", ""),
                "elapsed_s": round(elapsed, 1),
                "prompt": task.get("prompt", ""),
            }

            if task["status"] == "running":
                return base
            if task["status"] == "completed":
                return {**base, "body_md": task.get("body_md", "")}
            if task["status"] == "error":
                return {**base, "error": task.get("error", "未知错误")}
            if task["status"] == "cancelled":
                return {**base, "error": "已取消"}
            return base

    @app.post("/api/draft/{draft_id}/agent-cancel")
    def draft_agent_cancel(draft_id: int):
        """Cancel a running agent task."""
        with _agent_tasks_lock:
            task = _agent_tasks.get(draft_id)
            if not task or task["status"] != "running":
                return JSONResponse(
                    {"ok": False, "error": "没有正在运行的 Agent 任务"},
                    status_code=404)
            # Mark cancelled before killing. If communicate() raises as a
            # consequence, the stale worker is forbidden from replacing this
            # state with an error or publishing its output.
            task["status"] = "cancelled"
            provider = task.get("provider")
        if provider:
            provider.cancel()
        return {"ok": True, "cancelled": True}

    @app.post("/api/draft/{draft_id}/agent-apply")
    def draft_agent_apply(draft_id: int, run_id: str = Form(...)):
        """Atomically apply one completed run and return editor-ready state."""
        with _agent_tasks_lock:
            task = _agent_tasks.get(draft_id)
            if not task:
                return JSONResponse(
                    {"ok": False, "error": "Agent 结果不存在或已清除"},
                    status_code=404)
            if task.get("run_id") != run_id:
                return JSONResponse(
                    {"ok": False, "error": "Agent 结果已过期，请使用最新一次运行结果"},
                    status_code=409)
            if task.get("status") != "completed":
                return JSONResponse(
                    {"ok": False,
                     "error": f"Agent 结果尚不可应用（当前状态：{task.get('status')}）"},
                    status_code=409)

            store = get_store()
            row = store.get_draft(draft_id)
            if not row or not row["draft_path"]:
                return JSONResponse(
                    {"ok": False, "error": "草稿不存在"}, status_code=404)
            src = config.data_dir / row["draft_path"]
            if src.exists():
                bak_path = src.with_name(src.name + ".agent-bak")
                bak_path.write_text(
                    src.read_text(encoding="utf-8"), encoding="utf-8")
            store.update_draft_body(
                draft_id,
                title_candidates=task["title_candidates"],
                body_md=task["body_md"],
                status=task["draft_status"],
                title_cn=task["title_cn"],
            )
            response = {
                "ok": True,
                "run_id": run_id,
                "body_md": task["body_md"],
                "title_candidates": task["title_candidates"],
                "title_cn": task["title_cn"],
                "status": task["draft_status"],
            }
            _agent_tasks.pop(draft_id, None)
            logger.info("agent-edit draft=%d run=%s applied", draft_id, run_id)
            return response

    @app.post("/api/draft/{draft_id}/agent-clear")
    def draft_agent_clear(draft_id: int):
        """Forget a finished (non-running) agent task so the panel resets."""
        with _agent_tasks_lock:
            task = _agent_tasks.get(draft_id)
            if task and task["status"] == "running":
                return JSONResponse(
                    {"ok": False, "error": "Agent 正在运行，无法清除"},
                    status_code=409)
            _agent_tasks.pop(draft_id, None)
        if task and task.get("workspace"):
            shutil.rmtree(task["workspace"], ignore_errors=True)
        return {"ok": True}

    @app.post("/api/draft/{draft_id}/agent-undo")
    def draft_agent_undo(draft_id: int):
        """Restore draft from the backup created before the last agent edit."""
        store = get_store()
        row = store.get_draft(draft_id)
        if not row or not row["draft_path"]:
            return JSONResponse({"ok": False, "error": "草稿不存在"}, status_code=404)
        src = config.data_dir / row["draft_path"]
        bak_path = src.with_name(src.name + ".agent-bak")
        if not bak_path.exists():
            return JSONResponse({"ok": False, "error": "没有可恢复的备份"}, status_code=404)
        # Restore backup
        src.write_text(bak_path.read_text(encoding="utf-8"), encoding="utf-8")
        bak_path.unlink(missing_ok=True)
        logger.info("agent-undo draft=%d restored from backup", draft_id)
        # Re-read the restored content and sync DB record
        import frontmatter as _fm_undo
        restored = _fm_undo.loads(src.read_text(encoding="utf-8"))
        restored_titles = restored.metadata.get("title_candidates", []) or []
        restored_body = restored.content
        restored_cn = restored.metadata.get("title_cn")
        store.update_draft_body(
            draft_id,
            title_candidates=restored_titles,
            body_md=restored_body,
            title_cn=restored_cn,
        )
        return {"ok": True, "body_md": restored_body}

    @app.post("/api/draft/{draft_id}/agent-cleanup")
    def draft_agent_cleanup(draft_id: int):
        """Remove the agent backup file (called on normal form save)."""
        store = get_store()
        row = store.get_draft(draft_id)
        if row and row["draft_path"]:
            bak_path = config.data_dir / row["draft_path"]
            bak_path = bak_path.with_name(bak_path.name + ".agent-bak")
            if bak_path.exists():
                bak_path.unlink()
                logger.info("agent-cleanup draft=%d backup removed", draft_id)
        return {"ok": True}

    @app.get("/images/{name}")
    def serve_image(name: str):
        path = config.images_dir / name
        if not path.exists():
            return HTMLResponse("not found", status_code=404)
        return FileResponse(str(path))

    @app.get("/videos/{folder}/{name}")
    def serve_video_asset(folder: str, name: str):
        # Serve audio/video/script assets from data/videos/<folder>/<name>.
        base = config.videos_dir.resolve()
        path = (base / folder / name).resolve()
        if base not in path.parents or not path.exists():
            return HTMLResponse("not found", status_code=404)
        return FileResponse(str(path))

    @app.get("/media/{folder}/{name}")
    def serve_media_asset(folder: str, name: str):
        # Serve localized article images/videos from data/media/<folder>/<name>.
        base = config.media_dir.resolve()
        path = (base / folder / name).resolve()
        if base not in path.parents or not path.exists():
            return HTMLResponse("not found", status_code=404)
        return FileResponse(str(path))

    @app.get("/avatar/{name}")
    def serve_avatar_asset(name: str):
        # Serve the digital-human presenter portrait from data/avatar/<name>.
        base = config.avatar_dir.resolve()
        path = (base / name).resolve()
        if base not in path.parents or not path.exists():
            return HTMLResponse("not found", status_code=404)
        return FileResponse(str(path))

    @app.post("/api/avatar/upload")
    async def api_avatar_upload(file: UploadFile = File(...)):
        """Upload the digital-human presenter portrait; saved under
        data/avatar/ and remembered in settings as avatar_image."""
        import uuid
        config.avatar_dir.mkdir(parents=True, exist_ok=True)
        ext = (Path(file.filename or "avatar.png").suffix or ".png").lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            ext = ".png"
        name = f"presenter-{uuid.uuid4().hex[:12]}{ext}"
        (config.avatar_dir / name).write_bytes(await file.read())
        get_store().set_setting("avatar_image", name)
        return {"ok": True, "avatar_image": name,
                "url": f"/avatar/{name}"}

    # ---- export / import config (sources + settings, no API keys) ----
    _EXPORT_SETTING_KEYS = [
        "llm_provider", "llm_model", "llm_api_base",
        "image_provider", "image_api_base", "image_model",
        "tts_provider", "tts_api_base", "tts_model", "tts_voice",
        "max_age_days", "max_per_source", "max_drafts", "download_workers",
        "video_fit", "video_brand_name",
        "sensitive_level", "sensitive_words",
        "promotion_footer", "seo_tags_enabled",
        "schedule_cron", "schedule_enabled",
    ]

    def _source_to_dict(s: SourceConfig) -> dict:
        entry = {"name": s.name, "type": s.type, "url": s.url,
                 "topics": s.topics, "enabled": s.enabled}
        if s.type == "scrape":
            entry["mode"] = s.mode
            if s.include_pattern:
                entry["include_pattern"] = s.include_pattern
            if s.exclude_pattern:
                entry["exclude_pattern"] = s.exclude_pattern
        return entry

    @app.get("/export")
    def export_config():
        cfg_feeds = load_feeds(feeds_path) if feeds_path.exists() \
            else FeedsConfig(topics=[], sources=[])
        store = get_store()
        settings = {}
        for k in _EXPORT_SETTING_KEYS:
            v = store.get_setting(k)
            if v is not None and str(v) != "":
                settings[k] = v
        payload = {
            "version": 1,
            "feeds": {
                "topics": [{"name": t.name, "keywords": t.keywords}
                           for t in cfg_feeds.topics],
                "sources": [_source_to_dict(s) for s in cfg_feeds.sources],
            },
            "settings": settings,
        }
        data = json.dumps(payload, ensure_ascii=False, indent=2)
        return Response(
            content=data, media_type="application/json",
            headers={"Content-Disposition":
                     "attachment; filename=media-agent-config.json"})

    @app.post("/import")
    async def import_config(file: UploadFile = File(...)):
        try:
            payload = json.loads((await file.read()).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return RedirectResponse(url="/settings?import_error=1",
                                    status_code=303)

        fd = payload.get("feeds") or {}
        topics = [Topic(name=t.get("name", ""), keywords=t.get("keywords", []))
                  for t in fd.get("topics", []) if t.get("name")]
        sources = []
        for s in fd.get("sources", []):
            if not s.get("url") or not s.get("name"):
                continue
            sources.append(SourceConfig(
                name=s["name"], type=s.get("type", "rss"), url=s["url"],
                topics=s.get("topics", []), mode=s.get("mode", "single"),
                include_pattern=s.get("include_pattern"),
                exclude_pattern=s.get("exclude_pattern"),
                enabled=s.get("enabled", True)))
        if topics or sources:
            save_feeds(FeedsConfig(topics=topics, sources=sources), feeds_path)

        store = get_store()
        for k, v in (payload.get("settings") or {}).items():
            if k.endswith("_api_key") or v is None:  # never import secrets
                continue
            store.set_setting(k, str(v))
        return RedirectResponse(url="/settings?imported=1", status_code=303)

    @app.get("/sources", response_class=HTMLResponse)
    def sources_page():
        return _serve_spa()

    @app.get("/api/feeds")
    def api_feeds():
        """Topics + sources as JSON for the SPA."""
        cfg = load_feeds(feeds_path) if feeds_path.exists() else None
        if cfg is None:
            return {"topics": [], "sources": []}
        return {
            "topics": [{"name": t.name, "keywords": t.keywords}
                       for t in cfg.topics],
            "sources": [{
                "name": s.name, "type": s.type, "url": s.url,
                "topics": s.topics, "mode": s.mode,
                "include_pattern": s.include_pattern,
                "exclude_pattern": s.exclude_pattern,
                "max_pages": s.max_pages, "render_js": s.render_js,
                "enabled": s.enabled,
            } for s in cfg.sources],
        }

    @app.post("/sources/add")
    def sources_add(name: str = Form(...), type: str = Form("rss"),
                    url: str = Form(...), topics: str = Form(""),
                    mode: str = Form("single"),
                    include_pattern: str = Form(""),
                    exclude_pattern: str = Form(""),
                    max_pages: str = Form("1"),
                    render_js: str = Form("")):
        topic_list = [t.strip() for t in topics.split(",") if t.strip()]
        proxy = _resolve_proxy()
        reachable = check_url_connectivity(url, proxy=proxy)
        def _do_add(cfg):
            src = SourceConfig(
                name=name, type=type, url=url, topics=topic_list, mode=mode,
                include_pattern=include_pattern or None,
                exclude_pattern=exclude_pattern or None,
                max_pages=_to_int(max_pages, 1),
                render_js=render_js in ("1", "on", "true"),
                enabled=reachable)
            cfg.add_source(src)
        update_feeds(feeds_path, _do_add)
        return RedirectResponse(url="/sources", status_code=303)

    def _append_sources(found, skip_connectivity=False):
        added = 0
        proxy = _resolve_proxy() if not skip_connectivity else None
        def _do_append(cfg):
            nonlocal added
            for s in found:
                if cfg.add_source(s) is not None:
                    if not skip_connectivity and not check_url_connectivity(s.url, proxy=proxy):
                        s.enabled = False
                    added += 1
        update_feeds(feeds_path, _do_append)
        return added

    @app.post("/sources/discover")
    def sources_discover(url: str = Form(...), topics: str = Form("")):
        topic_list = [t.strip() for t in topics.split(",") if t.strip()]
        _append_sources(discover_from_url(url, topics=topic_list))
        return RedirectResponse(url="/sources", status_code=303)

    @app.post("/sources/search")
    def sources_search(keyword: str = Form(...), topics: str = Form("")):
        topic_list = [t.strip() for t in topics.split(",") if t.strip()]
        _append_sources(discover_from_keyword(keyword, topics=topic_list))
        return RedirectResponse(url="/sources", status_code=303)

    # ---- hotness ranking ----

    @app.post("/sources/refresh-hotness")
    def sources_refresh_hotness():
        store = get_store()
        cfg = load_feeds(feeds_path) if feeds_path.exists() else None
        try:
            data = compute_hotness(store, feeds_cfg=cfg,
                                   provider=_resolve_provider())
        except Exception:
            # Fallback: store-only with no provider
            data = compute_hotness(store)
        store.set_setting("hotness_data", json.dumps(data))
        store.set_setting("hotness_updated_at",
                          datetime.now(timezone.utc).isoformat())
        return {"status": "ok", **data}

    # ---- smart topic recommendation ----

    def _resolve_proxy() -> str | None:
        """Resolve the fetch proxy from Config, or None if not configured."""
        try:
            rc = _current_config(get_store())
            return rc.fetch_proxy
        except Exception:
            return None

    def _resolve_provider(rc: Config | None = None) -> LLMProvider:
        """Get LLM provider for rewriting, honoring rewrite_priority."""
        if rc is None:
            rc = _current_config(get_store())
        return get_rewrite_provider(
            rc.llm_provider, rc.llm_api_key,
            rc.llm_model, llm_api_base=rc.llm_api_base,
            cli_tool=rc.cli_tool, timeout=rc.cli_timeout,
            llm_timeout=rc.llm_timeout,
            priority=rc.rewrite_priority)

    def _build_tts(rc, tts_provider=None, tts_voice=None):
        provider = tts_provider if tts_provider is not None else rc.tts_provider
        voice = tts_voice if tts_voice is not None else rc.tts_voice
        # cosyvoice: resolve the selected voice id to its reference sample
        # and pass as speaker_wav.
        if provider == "cosyvoice":
            sp = voice_sample_path(config, voice)
            return get_tts_provider("cosyvoice", voice=str(sp) if sp else None,
                                    instruct=rc.tts_instruct,
                                    data_dir=config.data_dir)
        return get_tts_provider(
            provider, base_url=rc.tts_api_base,
            api_key=rc.tts_api_key, model=rc.tts_model, voice=voice,
            rate=rc.tts_rate, pitch=rc.tts_pitch)

    @app.post("/sources/topics/suggest")
    def topics_suggest(themes: str = Form(...)):
        err = LG.require(license_mgr, LF.AUTO_DISCOVER)
        if err:
            return err
        theme_list = [t.strip() for t in re.split(r"[,，\n]", themes) if t.strip()]
        try:
            subtopics = suggest_subtopics(theme_list, _resolve_provider())
        except Exception as exc:
            return {"themes": theme_list, "subtopics": [], "error": str(exc)}
        return {"themes": theme_list, "subtopics": subtopics}

    @app.post("/sources/topics/keywords")
    def topics_keywords(subtopic: str = Form(...)):
        err = LG.require(license_mgr, LF.AUTO_DISCOVER)
        if err:
            return err
        try:
            keywords = suggest_keywords(subtopic.strip(), _resolve_provider())
        except Exception as exc:
            return {"subtopic": subtopic.strip(), "keywords": [], "error": str(exc)}
        return {"subtopic": subtopic.strip(), "keywords": keywords}

    @app.post("/sources/suggest-sources")
    def sources_suggest_frontier(topic: str = Form(...)):
        err = LG.require(license_mgr, LF.AUTO_DISCOVER)
        if err:
            return err
        try:
            candidates = suggest_sources(topic.strip(), _resolve_provider())
        except Exception as exc:
            return {"topic": topic.strip(), "candidates": [], "error": str(exc)}
        return {"topic": topic.strip(), "candidates": candidates}

    @app.post("/sources/discover-add")
    def sources_discover_add(url: str = Form(...), topics: str = Form("")):
        topic_list = [t.strip() for t in topics.split(",") if t.strip()]
        found = discover_from_url(url, topics=topic_list)
        if not found:
            return {"url": url, "added": 0, "found": 0,
                    "status": "fetch_failed"}
        added = _append_sources(found)
        status = "added" if added else "exists"
        return {"url": url, "added": added, "found": len(found),
                "status": status}

    @app.post("/sources/batch-discover-add")
    async def sources_batch_discover_add(request: Request):
        body = await request.json()
        items = body.get("items", [])
        found = []
        for item in items:
            name = item.get("name", "")
            url = item.get("url", "")
            if not url:
                continue
            topics = item.get("topics", [])
            if isinstance(topics, str):
                topics = [t.strip() for t in topics.split(",") if t.strip()]
            # Accept an optional per-item type (default "rss" for backward
            # compat). Scrape candidates need mode="list" so the collector
            # scrapes the article-listing page.
            typ = item.get("type", "rss") or "rss"
            found.append(SourceConfig(
                name=name, type=typ, url=url, topics=topics,
                mode="list" if typ == "scrape" else "single"))
        added = _append_sources(found)
        return {"added": added, "total": len(found)}

    @app.post("/sources/batch-add")
    async def sources_batch_add(request: Request):
        body = await request.json()
        items = body.get("items", [])
        found = []
        for item in items:
            name = item.get("name", "").strip()
            url = item.get("url", "").strip()
            if not url:
                continue
            typ = item.get("type", "rss") or "rss"
            topics = item.get("topics", [])
            if isinstance(topics, str):
                topics = [t.strip() for t in topics.split(",") if t.strip()]
            mode = item.get("mode", "single") or "single"
            found.append(SourceConfig(
                name=name, type=typ, url=url,
                topics=topics, mode=mode,
                include_pattern=item.get("include_pattern") or None,
                exclude_pattern=item.get("exclude_pattern") or None,
                max_pages=_to_int(item.get("max_pages"), 1),
                render_js=item.get("render_js", False) in (True, "1", "on", "true"),
            ))
        added = _append_sources(found)
        return {"added": added, "total": len(found)}

    # ---- topic management ----

    @app.post("/sources/topics/add")
    def topics_add(name: str = Form(...), keywords: str = Form("")):
        kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
        def _do_add(cfg):
            cfg.add_topic(Topic(name=name.strip(), keywords=kw_list))
        update_feeds(feeds_path, _do_add)
        return RedirectResponse(url="/sources", status_code=303)

    @app.post("/sources/topics/edit")
    def topics_edit(old_name: str = Form(...), name: str = Form(...),
                    keywords: str = Form("")):
        kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
        _old = old_name.strip()
        _name = name.strip()
        def _do_edit(cfg):
            for t in cfg.topics:
                if t.name == _old:
                    t.name = _name
                    t.keywords = kw_list
                    break
        update_feeds(feeds_path, _do_edit)
        return RedirectResponse(url="/sources", status_code=303)

    @app.post("/sources/topics/delete")
    def topics_delete(name: str = Form(...)):
        _name = name.strip()
        def _do_delete(cfg):
            cfg.topics = [t for t in cfg.topics if t.name != _name]
        update_feeds(feeds_path, _do_delete)
        return RedirectResponse(url="/sources", status_code=303)

    # ---- source management ----

    @app.post("/sources/edit")
    def sources_edit(url: str = Form(...), name: str = Form(...),
                     type: str = Form("rss"), topics: str = Form(""),
                     mode: str = Form("single"),
                     include_pattern: str = Form(""),
                     exclude_pattern: str = Form(""),
                     max_pages: str = Form("1"),
                     render_js: str = Form(""),
                     new_url: str = Form("")):
        topic_list = [t.strip() for t in topics.split(",") if t.strip()]
        _url = url.strip()
        _new_url = new_url.strip() or _url
        def _do_edit(cfg):
            for s in cfg.sources:
                if s.url == _url:
                    s.url = _new_url
                    s.name = name.strip()
                    s.type = type.strip()
                    s.topics = topic_list
                    s.mode = mode.strip()
                    s.include_pattern = include_pattern.strip() or None
                    s.exclude_pattern = exclude_pattern.strip() or None
                    s.max_pages = _to_int(max_pages, 1)
                    s.render_js = render_js in ("1", "on", "true")
                    break
        update_feeds(feeds_path, _do_edit)
        return RedirectResponse(url="/sources", status_code=303)

    @app.post("/sources/delete")
    def sources_delete(url: str = Form(...)):
        _url = url.strip()
        def _do_delete(cfg):
            cfg.sources = [s for s in cfg.sources if s.url != _url]
        update_feeds(feeds_path, _do_delete)
        return RedirectResponse(url="/sources", status_code=303)

    @app.post("/sources/delete-batch")
    async def sources_delete_batch(request: Request):
        body = await request.json()
        urls = body.get("items", [])
        if not urls:
            return {"deleted": 0}
        url_set = set(u.strip() for u in urls)
        deleted = 0
        def _do_delete(cfg):
            nonlocal deleted
            before = len(cfg.sources)
            cfg.sources = [s for s in cfg.sources if s.url not in url_set]
            deleted = before - len(cfg.sources)
        update_feeds(feeds_path, _do_delete)
        return {"deleted": deleted}

    @app.post("/sources/topics/delete-batch")
    async def topics_delete_batch(request: Request):
        body = await request.json()
        names = body.get("items", [])
        if not names:
            return {"deleted": 0}
        name_set = set(n.strip() for n in names)
        deleted = 0
        def _do_delete(cfg):
            nonlocal deleted
            before = len(cfg.topics)
            cfg.topics = [t for t in cfg.topics if t.name not in name_set]
            deleted = before - len(cfg.topics)
        update_feeds(feeds_path, _do_delete)
        return {"deleted": deleted}

    @app.post("/sources/toggle")
    def sources_toggle(url: str = Form(...)):
        _url = url.strip()
        def _do_toggle(cfg):
            for s in cfg.sources:
                if s.url == _url:
                    s.enabled = not s.enabled
                    break
        update_feeds(feeds_path, _do_toggle)
        return RedirectResponse(url="/sources", status_code=303)

    @app.post("/sources/toggle-batch")
    async def sources_toggle_batch(request: Request):
        body = await request.json()
        urls = body.get("items", [])
        enabled = body.get("enabled")
        if not urls or enabled is None:
            return {"updated": 0}
        url_set = set(u.strip() for u in urls)
        updated = 0
        def _do_toggle(cfg):
            nonlocal updated
            for s in cfg.sources:
                if s.url in url_set and s.enabled != enabled:
                    s.enabled = enabled
                    updated += 1
        update_feeds(feeds_path, _do_toggle)
        return {"updated": updated}

    @app.post("/sources/check-reachability")
    def sources_check_reachability():
        """Check all enabled sources with 3 retries each; disable unreachable."""
        from concurrent.futures import ThreadPoolExecutor
        def _check_with_retries(url: str, retries: int = 3, timeout: float = 10.0) -> bool:
            for attempt in range(retries):
                if check_url_connectivity(url, timeout=timeout, proxy=_resolve_proxy()):
                    return True
            return False

        cfg = load_feeds(feeds_path)
        enabled_sources = [s for s in cfg.sources if s.enabled]
        if not enabled_sources:
            return {"total": 0, "disabled": 0, "results": []}

        total = len(enabled_sources)
        check_progress.update(running=True, current=0, total=total, disabled=0, results=[])
        results: list[dict] = []

        def _check(s):
            ok = _check_with_retries(s.url)
            return {"name": s.name, "url": s.url, "reachable": ok}

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_check, s): s for s in enabled_sources}
            for future in futures:
                r = future.result()
                results.append(r)
                check_progress["current"] = len(results)
                check_progress["disabled"] = sum(1 for x in results if not x["reachable"])
                check_progress["results"] = results

        disabled_urls = [r["url"] for r in results if not r["reachable"]]
        if disabled_urls:
            disabled_set = set(disabled_urls)
            def _do_disable(cfg):
                for s in cfg.sources:
                    if s.url in disabled_set:
                        s.enabled = False
            update_feeds(feeds_path, _do_disable)

        check_progress.update(running=False, results=results)
        return {
            "total": total,
            "disabled": len(disabled_urls),
            "results": results,
        }

    @app.get("/sources/check-reachability/progress")
    async def sources_check_reachability_progress():
        return check_progress

    def _parallel_llm_timeout(item_count: int, *, workers: int = 5,
                              per_batch_sec: int = 60, minimum: int = 120) -> float:
        """Wall-clock budget for parallel LLM fan-out (as_completed timeout)."""
        if item_count <= 0:
            return minimum
        batch_workers = min(item_count, workers)
        batches = math.ceil(item_count / batch_workers)
        return max(minimum, batches * per_batch_sec)

    # ---- auto-discover (single endpoint, server-side parallelism) ----
    auto_discover_progress = {
        "running": False, "step": 0, "total_steps": 5,
        "step_name": "", "detail": "",
        "current": 0, "total": 0,
        "result": None, "error": None,
        "started_at": 0.0, "updated_at": 0.0,
    }

    @app.post("/sources/auto-discover")
    def sources_auto_discover(themes: str = Form(...)):
        err = LG.require(license_mgr, LF.AUTO_DISCOVER)
        if err:
            return err
        from concurrent.futures import (
            ThreadPoolExecutor, as_completed,
            TimeoutError as FuturesTimeoutError,
        )

        p = auto_discover_progress
        rc = _current_config(get_store())
        provider = _resolve_provider(rc)

        def _provider_label(prov) -> str:
            """Human name for the backend actually chosen (honors rewrite_priority
            AND real availability — e.g. shows 'Agent' if an LLM was requested
            but unavailable and it fell back)."""
            cls = type(prov).__name__
            if cls == "CLIProvider":
                return "Agent"
            if cls == "MockProvider":
                return "Mock"
            return "大模型"
        prov_label = _provider_label(provider)
        if (rc.rewrite_priority or "agent").strip().lower() == "llm" \
                and prov_label != "大模型":
            logger.warning(
                "[auto-discover] rewrite_priority=llm but LLM provider '%s' "
                "is unavailable (no key/unreachable/mock); using %s instead",
                rc.llm_provider, prov_label)

        theme_list = [t.strip() for t in re.split(r"[,，\n]", themes) if t.strip()]
        if not theme_list:
            p.update(running=False, error="请输入主题")
            return {"error": "请输入主题"}

        _now = time.time()
        p.update(running=True, step=1, step_name="推荐子主题",
                 detail=f"主题：{', '.join(theme_list)}",
                 current=0, total=0, result=None, error=None,
                 started_at=_now, updated_at=_now)
        logger.info("[auto-discover] step 1/5: suggest subtopics for %s", theme_list)

        def _log_failure(step: int, what: str, name: str, exc: BaseException) -> None:
            logger.warning(
                "[auto-discover] step %d: %s failed for %s: %s",
                step, what, name, exc, exc_info=True)

        def _bump(**kw) -> None:
            """Heartbeat + optional progress fields so the UI can detect stalls."""
            kw["updated_at"] = time.time()
            p.update(kw)

        def _run_bounded(fn, *args, timeout: float):
            """Run a single blocking call with a hard wall-clock timeout so a
            hung LLM/agent can never freeze the whole pipeline."""
            solo = ThreadPoolExecutor(max_workers=1)
            try:
                fut = solo.submit(fn, *args)
                return fut.result(timeout=timeout)
            finally:
                solo.shutdown(wait=False, cancel_futures=True)

        class _ChatTimeout:
            """Wrap a provider so each chat() call gets a bounded timeout.

            CLI agents default to a very long per-call timeout (cli_timeout,
            ~500s); during auto-discover we want each recommendation call to
            fail fast so one slow topic can't starve the whole batch.
            """
            def __init__(self, inner, timeout: float):
                self._inner = inner
                self._timeout = timeout

            def chat(self, messages, **opts):
                opts.setdefault("timeout", self._timeout)
                return self._inner.chat(messages, **opts)

        def _collect_parallel(fn, items, *, workers, overall_timeout,
                              on_progress, label):
            """Run fn(item) in parallel, emitting a heartbeat every few seconds
            (even while waiting) so the UI never looks frozen.  Returns
            (results, unfinished) where results maps item-INDEX→value-or-Exception
            (indexed so unhashable items like dicts are supported) and
            unfinished lists items that didn't finish before the deadline.
            """
            from concurrent.futures import wait, FIRST_COMPLETED
            results: dict[int, object] = {}
            if not items:
                return results, []
            pool = ThreadPoolExecutor(max_workers=min(len(items), workers))
            try:
                fut_to_idx = {pool.submit(fn, it): i for i, it in enumerate(items)}
                pending = set(fut_to_idx)
                deadline = time.time() + overall_timeout
                while pending and time.time() < deadline:
                    done, pending = wait(pending, timeout=3,
                                         return_when=FIRST_COMPLETED)
                    for fut in done:
                        idx = fut_to_idx[fut]
                        try:
                            results[idx] = fut.result()
                        except Exception as exc:
                            results[idx] = exc
                    on_progress(len(results), len(items))
                unfinished = [items[fut_to_idx[f]] for f in pending]
                for f in pending:
                    f.cancel()
                if unfinished:
                    logger.warning("[auto-discover] %s: %d/%d finished, %d timed out",
                                   label, len(results), len(items), len(unfinished))
                return results, unfinished
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

        try:
            # ── Step 1: suggest subtopics (1 LLM call, bounded) ──
            try:
                subtopics = _run_bounded(
                    suggest_subtopics, theme_list, provider,
                    timeout=_parallel_llm_timeout(1))
            except FuturesTimeoutError:
                _bump(running=False, step=1, step_name="推荐子主题",
                      detail="推荐子主题超时", error="推荐子主题超时（LLM/Agent 无响应）")
                logger.warning("[auto-discover] step 1: suggest_subtopics timed out")
                return {"error": "推荐子主题超时"}
            except Exception as exc:
                _bump(running=False, step=1, step_name="推荐子主题",
                      detail=f"推荐子主题失败：{exc}", error=str(exc))
                logger.warning("[auto-discover] step 1: suggest_subtopics failed: %s",
                               exc, exc_info=True)
                return {"error": str(exc)}
            if not subtopics:
                _bump(running=False, step=1, step_name="推荐子主题",
                      detail="没有推荐出子主题", error="没有推荐出子主题")
                logger.info("[auto-discover] step 1: no subtopics returned")
                return {"error": "没有推荐出子主题"}
            logger.info("[auto-discover] step 1: got %d subtopics: %s",
                        len(subtopics), subtopics)

            # ── Step 2: fetch keywords for each subtopic (parallel LLM calls) ──
            _bump(step=2, step_name="获取关键词",
                  detail=f"共 {len(subtopics)} 个子主题",
                  current=0, total=len(subtopics))
            logger.info("[auto-discover] step 2/5: fetch keywords for %d subtopics", len(subtopics))

            # ONE batched agent call for ALL subtopics (avoids ~N cold-start
            # subprocess spawns that make CLI agents time out). Run it via a
            # single-item collector so the UI heartbeat keeps ticking.
            kw_provider = _ChatTimeout(provider, timeout=300)
            kw_batch_res, _ = _collect_parallel(
                lambda _: suggest_keywords_batch(subtopics, kw_provider),
                [None], workers=1, overall_timeout=320,
                on_progress=lambda n, m: _bump(
                    detail=f"正在向 {prov_label} 批量获取 {len(subtopics)} 个子主题的关键词…"),
                label="step 2")
            kw_map = kw_batch_res.get(0)
            if isinstance(kw_map, Exception):
                _log_failure(2, "keywords(batch)", f"{len(subtopics)} 个子主题", kw_map)
                kw_map = {}
            kw_map = kw_map or {}

            kw_results: list[dict] = [
                {"name": st, "keywords": kw_map.get(st, [])} for st in subtopics
            ]

            # ── Step 3: add topics to feeds.yaml ──
            _bump(step=3, step_name="添加为主题",
                  detail=f"共 {len(kw_results)} 个子主题",
                  current=0, total=len(kw_results))
            logger.info("[auto-discover] step 3/5: add %d topics", len(kw_results))

            # Collect all topics with keywords, then write them in a SINGLE
            # atomic update.  Doing one replace instead of N tight-loop
            # replaces avoids repeatedly tripping Windows file locks
            # (antivirus/indexer) on feeds.yaml.
            pending = [it for it in kw_results if it["keywords"]]
            for it in kw_results:
                if not it["keywords"]:
                    logger.info("[auto-discover] step 3: skip %s (no keywords)", it["name"])

            added_names: list[str] = []
            if pending:
                def _do_add_all(cfg, _items=pending, _added=added_names):
                    for it in _items:
                        kw_list = [k.strip() for k in it["keywords"] if str(k).strip()]
                        if cfg.add_topic(Topic(name=it["name"], keywords=kw_list)) is not None:
                            _added.append(it["name"])
                try:
                    update_feeds(feeds_path, _do_add_all)
                    _bump(current=len(kw_results),
                          detail=f"已添加 {len(added_names)} 个主题")
                    for name in added_names:
                        logger.info("[auto-discover] step 3: added topic %s", name)
                except Exception as exc:
                    added_names.clear()
                    _log_failure(3, "add topics", f"{len(pending)} 个主题", exc)

            if not added_names:
                _bump(running=False, step=3, step_name="添加为主题",
                      detail="没有新增主题",
                      result={"added_topics": 0, "added_sources": 0,
                              "total": 0, "skipped": 0})
                logger.info("[auto-discover] step 3: no topics added")
                return {"error": "没有新增主题"}

            logger.info("[auto-discover] step 3: added %d topics: %s", len(added_names), added_names)

            # ── Step 4: discover sources ──
            # 4a) ONE batched agent call for ALL topics → raw {topic:[{name,url}]}.
            # 4b) Resolve real RSS feeds over the network in parallel (no agent).
            all_candidates: list[dict] = []
            seen_urls: set[str] = set()
            _bump(step=4, step_name="发现来源",
                  detail=f"正在向 {prov_label} 批量获取 {len(added_names)} 个主题的候选来源…",
                  current=0, total=len(added_names))
            logger.info("[auto-discover] step 4/5: batch suggest sources for %d topics",
                        len(added_names))

            # suggest_source_names_batch now chunks internally (one agent call
            # per _SOURCE_BATCH_CHUNK topics) so a single oversized/failed call
            # can't zero every topic. The per-call timeout bounds ONE chunk;
            # the overall budget must cover all chunks running sequentially,
            # plus margin. The single-item collector still heartbeats every ~3s
            # so the UI never looks frozen during the long chunked call.
            src_n_chunks = max(1, math.ceil(len(added_names) / _SOURCE_BATCH_CHUNK))
            src_chunk_timeout = 150.0
            src_overall_timeout = src_n_chunks * src_chunk_timeout + 90.0
            src_provider = _ChatTimeout(provider, timeout=src_chunk_timeout)
            logger.info("[auto-discover] step 4: %d topics → %d chunk(s), "
                        "per-chunk timeout=%.0fs, overall=%.0fs",
                        len(added_names), src_n_chunks, src_chunk_timeout,
                        src_overall_timeout)
            name_res, _ = _collect_parallel(
                lambda _: suggest_source_names_batch(added_names, src_provider),
                [None], workers=1, overall_timeout=src_overall_timeout,
                on_progress=lambda n, m: _bump(
                    detail=f"正在向 {prov_label} 批量获取 {len(added_names)} 个主题的候选来源…"),
                label="step 4 llm")
            name_map = name_res.get(0)
            batch_note = ""
            if isinstance(name_map, Exception):
                _log_failure(4, "sources(batch)", f"{len(added_names)} 个主题", name_map)
                batch_note = f"{prov_label} 调用失败：{name_map}"
                name_map = {}
            elif name_map is None:
                logger.warning("[auto-discover] step 4: batch source call timed out "
                               "(no result within budget)")
                batch_note = f"{prov_label} 调用超时（未在预算内返回）"
                name_map = {}
            name_map = name_map or {}

            raw_total = sum(len(name_map.get(t) or []) for t in added_names)
            raw_with_url = sum(
                1 for t in added_names for c in (name_map.get(t) or [])
                if str((c or {}).get("url") or "").startswith("http"))
            topics_with_cands = sum(1 for t in added_names if name_map.get(t))
            logger.info("[auto-discover] step 4: batch returned candidates for "
                        "%d/%d topics (%d raw candidates, %d with usable URL)",
                        topics_with_cands, len(added_names), raw_total,
                        raw_with_url)

            # Resolve candidates into registered sources via the SAME helper the
            # manual per-topic suggest_sources uses, so the one-click flow yields
            # identical results. _resolve_sources is now network-free (it just
            # keeps the correct URL and tags a cheap rss/scrape type — the
            # scraper decides what to crawl), so this is CPU-only and returns
            # near-instantly; resolve all topics inline (no parallel network
            # phase / long timeouts needed). The UI still heartbeats here and
            # reachability is validated in step 5.
            resolve_topics = list(added_names)
            _bump(detail=f"正在解析 {len(resolve_topics)} 个主题的来源…",
                  current=0, total=len(resolve_topics))
            for i, t in enumerate(resolve_topics, 1):
                for it in _resolve_sources(t, name_map.get(t) or []):
                    url = (it.get("url") or "").rstrip("/")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        all_candidates.append(
                            {"name": it["name"], "url": it["url"],
                             "type": it.get("type", "scrape"), "topics": [t]})
                _bump(current=i, detail=f"解析来源 [{i}/{len(resolve_topics)}]")

            logger.info("[auto-discover] step 4: resolved %d sources from %d topics",
                        len(all_candidates), len(resolve_topics))

            if not all_candidates:
                if raw_total == 0:
                    reason = batch_note or f"{prov_label} 未返回候选来源"
                elif raw_with_url == 0:
                    reason = "候选来源均缺少可用 URL"
                else:
                    reason = "候选均未解析出可用来源"
                _bump(running=False, step=4, step_name="发现来源",
                      detail=f"没有发现来源（{reason}）",
                      result={"added_topics": len(added_names),
                              "added_sources": 0, "total": 0, "skipped": 0})
                logger.info("[auto-discover] step 4: no candidates found (%s)", reason)
                return {"added_topics": len(added_names),
                        "added_sources": 0, "total": 0}

            logger.info("[auto-discover] step 4: found %d unique sources", len(all_candidates))

            # ── Step 5: validate + batch add sources ──
            _bump(step=5, step_name="验证并添加来源",
                  detail=f"正在验证 {len(all_candidates)} 个来源的可达性",
                  current=0, total=len(all_candidates))
            logger.info("[auto-discover] step 5/5: validate & add %d sources", len(all_candidates))

            # Validate reachability in parallel before adding
            proxy = _resolve_proxy()
            validated: list[SourceConfig] = []
            skipped: list[dict] = []

            def _check_source(c):
                try:
                    return check_url_connectivity(c["url"], timeout=8.0, proxy=proxy)
                except Exception:
                    return False

            # Each check is bounded at 8s; give the whole batch generous but
            # finite wall-clock headroom so a stalled socket can't freeze us.
            check_workers = min(len(all_candidates), 10)
            check_timeout = max(60.0, math.ceil(len(all_candidates) / check_workers) * 30)
            check_map, check_unfinished = _collect_parallel(
                _check_source, all_candidates, workers=check_workers,
                overall_timeout=check_timeout,
                on_progress=lambda n, m: _bump(current=n,
                                               detail=f"验证来源 [{n}/{m}]"),
                label="step 5")

            for i, c in enumerate(all_candidates):
                res = check_map.get(i)
                reachable = res is True
                # Unreachable only when we actually got a False result; if the
                # check didn't finish, keep the source rather than dropping it.
                if reachable or res is None:
                    _ctype = c.get("type", "rss")
                    validated.append(SourceConfig(
                        name=c["name"], type=_ctype, url=c["url"],
                        topics=c["topics"],
                        mode="list" if _ctype == "scrape" else "single"))
                    if res is None:
                        logger.info("[auto-discover] step 5: keeping unverified %s (%s)",
                                    c["name"], c["url"])
                else:
                    skipped.append({"name": c["name"], "url": c["url"]})
                    logger.info("[auto-discover] step 5: skipping unreachable %s (%s)",
                                c["name"], c["url"])

            if skipped:
                logger.info("[auto-discover] step 5: %d sources skipped (unreachable), "
                            "%d validated", len(skipped), len(validated))

            added_count = _append_sources(validated, skip_connectivity=True) if validated else 0

            _bump(running=False, step=5, step_name="完成",
                  detail=f"新增 {added_count} 个来源（跳过 {len(skipped)} 个不可达来源）",
                  current=len(all_candidates), total=len(all_candidates),
                  result={"added_topics": len(added_names),
                          "added_sources": added_count,
                          "total": len(all_candidates),
                          "skipped": len(skipped)})
            logger.info("[auto-discover] step 5: added %d sources, skipped %d unreachable. DONE.",
                        added_count, len(skipped))
            return {"added_topics": len(added_names),
                    "added_sources": added_count,
                    "total": len(all_candidates)}

        except Exception as exc:
            p.update(running=False, error=str(exc), updated_at=time.time())
            logger.error("[auto-discover] failed: %s", exc, exc_info=True)
            return {"error": str(exc)}

    @app.get("/sources/auto-discover/progress")
    async def sources_auto_discover_progress():
        return auto_discover_progress

    @app.post("/sources/fix-disabled")
    def sources_fix_disabled():
        """For each disabled source, search for the correct URL and update it."""
        from concurrent.futures import ThreadPoolExecutor
        from app.discovery import search_web
        from urllib.parse import unquote

        cfg = load_feeds(feeds_path)
        disabled_sources = [s for s in cfg.sources if not s.enabled]
        if not disabled_sources:
            return {"total": 0, "fixed": 0, "removed": 0, "results": []}

        results: list[dict] = []
        fixed_count = 0
        removed_count = 0
        skipped_count = 0

        def _fix_source(s):
            nonlocal fixed_count, removed_count
            name = s.name
            old_url = s.url
            # First: re-check if original URL is now reachable (may have been transient)
            if check_url_connectivity(old_url, timeout=10.0, proxy=_resolve_proxy()):
                return {"name": name, "old_url": old_url, "new_url": old_url, "status": "fixed"}
            # Search for correct URL
            for query in (f"{name} blog", f"{name} news", f"{name}"):
                candidates = search_web(query, max_results=5)
                for cand in candidates:
                    if check_url_connectivity(cand, timeout=8.0, proxy=_resolve_proxy()):
                        return {"name": name, "old_url": old_url, "new_url": cand, "status": "fixed"}
            # No reachable URL found — keep disabled, do NOT delete
            return {"name": name, "old_url": old_url, "new_url": None, "status": "skipped"}

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_fix_source, s): s for s in disabled_sources}
            for future in futures:
                results.append(future.result())

        # Apply changes
        def _apply(cfg):
            nonlocal fixed_count, removed_count, skipped_count
            for r in results:
                if r["status"] == "fixed":
                    for s in cfg.sources:
                        if s.url == r["old_url"]:
                            s.url = r["new_url"]
                            s.enabled = True
                            fixed_count += 1
                            break
                elif r["status"] == "removed":
                    cfg.sources = [s for s in cfg.sources if s.url != r["old_url"]]
                    removed_count += 1
                elif r["status"] == "skipped":
                    skipped_count += 1
        update_feeds(feeds_path, _apply)

        return {
            "total": len(disabled_sources),
            "fixed": fixed_count,
            "skipped": skipped_count,
            "removed": removed_count,
            "results": results,
        }

    def _search_create_public_state() -> dict:
        with search_create_lock:
            return {
                "running": search_create_state["running"],
                "status": search_create_state["status"],
                "stage": search_create_state["stage"],
                "detail": search_create_state["detail"],
                "current": search_create_state["current"],
                "total": search_create_state["total"],
                "stats": dict(search_create_state["stats"]),
                "logs": list(search_create_state["logs"]),
                "error": search_create_state["error"],
                "draft_id": search_create_state["draft_id"],
                "op_id": search_create_state["op_id"],
                "started_at": search_create_state["started_at"],
                "finished_at": search_create_state["finished_at"],
                "request": dict(search_create_state["request"]),
                "topic": search_create_state["request"].get("topic"),
            }

    @app.post("/api/search-create")
    async def api_search_create(request: Request):
        denied = LG.require(license_mgr, LF.SEARCH_CREATE)
        if denied is not None:
            return denied
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("请求体必须为 JSON 对象")
            topic = str(payload.get("topic") or "").strip()
            lang = str(payload.get("lang") or "zh")
            time_range_days = int(payload.get("time_range_days", 30))
            ref_count = int(payload.get("ref_count", 10))
            style_id = str(payload.get("style_id") or "").strip() or None
            raw_engines = payload.get(
                "engines", ["bing", "duckduckgo", "google", "brave"])
            if not isinstance(raw_engines, list):
                raise ValueError("搜索引擎必须为数组")
            engines = [str(engine) for engine in raw_engines]
            options = SearchCreateOptions(
                topic=topic,
                lang=lang,
                time_range_days=time_range_days,
                ref_count=ref_count,
                style_id=style_id,
                engines=engines,
                workers=config.workers,
                proxy=config.fetch_proxy,
            )
            options.validate()
            validation_store = get_store()
            if style_id and rewrite_styles.get_style(
                    style_id, validation_store) is None:
                raise ValueError("写作风格不存在")
            request_snapshot = {
                "topic": options.topic,
                "lang": options.lang,
                "time_range_days": options.time_range_days or 0,
                "ref_count": options.ref_count,
                "style_id": options.style_id,
                "engines": list(options.engines),
            }
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)}, status_code=400)

        with search_create_lock:
            if search_create_state["running"]:
                return JSONResponse(
                    {"ok": False, "error": "已有搜索创作任务正在进行"},
                    status_code=409)
            search_create_state.update(
                running=True, status="running", stage="expand",
                detail="任务已启动", current=0, total=0, stats={}, logs=[],
                error=None, draft_id=None, op_id=None,
                started_at=datetime.now(timezone.utc).isoformat(),
                finished_at=None, cancel_requested=False,
                request=request_snapshot)

        def worker() -> None:
            op_conn = _db_conn()
            op_id = start_op(op_conn, "search_create", 0)
            with search_create_lock:
                search_create_state["op_id"] = op_id
            store = Store(op_conn, config)
            run_config = _current_config(store)
            options.workers = run_config.workers
            options.proxy = run_config.fetch_proxy
            provider = get_rewrite_provider(
                run_config.llm_provider, run_config.llm_api_key,
                run_config.llm_model,
                llm_api_base=run_config.llm_api_base,
                cli_tool=run_config.cli_tool,
                timeout=run_config.cli_timeout,
                llm_timeout=run_config.llm_timeout,
                priority=run_config.rewrite_priority)
            style = rewrite_styles.resolve_style(options.style_id, store)
            last_detail = None

            def cancelled() -> bool:
                with search_create_lock:
                    return bool(search_create_state["cancel_requested"])

            def progress(event: dict) -> None:
                nonlocal last_detail
                detail = str(event.get("detail") or "")
                stats = dict(event.get("stats") or {})
                with search_create_lock:
                    search_create_state.update(
                        stage=event.get("stage"),
                        detail=detail,
                        current=int(event.get("current") or 0),
                        total=int(event.get("total") or 0),
                        stats=stats,
                    )
                    if detail and detail != last_detail:
                        line = (
                            f"{datetime.now().strftime('%H:%M:%S')} {detail}")
                        search_create_state["logs"].append(line)
                        if len(search_create_state["logs"]) > 500:
                            del search_create_state["logs"][:-500]
                if detail and detail != last_detail:
                    _op_log(op_conn, op_id, detail)
                    last_detail = detail
                update_op(op_conn, op_id, stats={
                    "stage": event.get("stage"),
                    "current": event.get("current", 0),
                    "total": event.get("total", 0),
                    **stats,
                })

            try:
                result = run_search_create(
                    store, provider, options, style=style,
                    progress=progress, should_stop=cancelled)
                draft_id = result.get("draft_id")
                with search_create_lock:
                    search_create_state.update(
                        status="done", draft_id=draft_id,
                        stats=dict(result.get("stats") or {}))
                update_op(op_conn, op_id, target_id=draft_id)
                finish_op(op_conn, op_id, result.get("stats"))
            except SearchCreateCancelled:
                with search_create_lock:
                    search_create_state.update(
                        status="cancelled", error=None,
                        detail="搜索创作已取消")
                cancel_op(op_conn, op_id)
            except ValueError as exc:
                logger.warning("search-create stopped: %s", exc)
                with search_create_lock:
                    search_create_state.update(
                        status="error", error=str(exc), detail="任务失败")
                fail_op(op_conn, op_id, str(exc))
            except Exception as exc:  # noqa: BLE001
                logger.exception("search-create failed")
                with search_create_lock:
                    search_create_state.update(
                        status="error", error=str(exc), detail="任务失败")
                fail_op(op_conn, op_id, str(exc))
            finally:
                with search_create_lock:
                    search_create_state.update(
                        running=False,
                        finished_at=datetime.now(timezone.utc).isoformat())
                op_conn.close()

        threading.Thread(
            target=worker, daemon=True, name="search-create").start()
        return {"ok": True, "started": True, **_search_create_public_state()}

    @app.get("/api/search-create/status")
    def api_search_create_status():
        state = _search_create_public_state()
        if state["status"] == "idle":
            conn = _db_conn()
            try:
                latest = get_latest_op(conn, "search_create")
            finally:
                conn.close()
            if latest is not None:
                op_stats = latest.get("stats") or {}
                state.update({
                    "status": latest["status"],
                    "running": latest["status"] == "running",
                    "stage": op_stats.get("stage"),
                    "current": op_stats.get("current", 0),
                    "total": op_stats.get("total", 0),
                    "stats": op_stats,
                    "logs": latest.get("logs") or [],
                    "error": latest.get("error"),
                    "draft_id": latest.get("target_id") or None,
                    "op_id": latest.get("id"),
                    "started_at": latest.get("started_at"),
                    "finished_at": latest.get("finished_at"),
                    "request": op_stats.get("request") or {
                        key: op_stats.get(key) for key in (
                            "topic", "lang", "time_range_days", "ref_count",
                            "style_id", "engines")
                        if op_stats.get(key) is not None
                    },
                    "topic": op_stats.get("topic"),
                })
        return {"ok": True, **state}

    @app.post("/api/search-create/cancel")
    def api_search_create_cancel():
        denied = LG.require(license_mgr, LF.SEARCH_CREATE)
        if denied is not None:
            return denied
        with search_create_lock:
            if not search_create_state["running"]:
                return JSONResponse(
                    {"ok": False, "error": "没有运行中的搜索创作任务"},
                    status_code=409)
            search_create_state["cancel_requested"] = True
            search_create_state["detail"] = "正在取消…"
        return {"ok": True, "cancel_requested": True}

    @app.post("/run")
    def trigger_run():
        with run_lock:
            if run_state["running"]:
                return {"started": False, "running": True,
                        "message": "已有运行正在进行"}
            run_state.update(running=True, error=None, stats={}, logs=[],
                             started_at=datetime.now().isoformat(timespec="seconds"),
                             finished_at=None, paused=False,
                             stop_requested=False, stopped=False)

        def worker():
            op_key = "run"
            op_conn = _db_conn()
            ops.begin(op_key, "run", 0, op_conn)
            try:
                _pausable_log("开始运行流水线")
                stats = run_now(progress=_pausable_log)
                with run_lock:
                    if stats:
                        run_state["stats"] = dict(stats)
                _pausable_log("运行成功结束", run_state["stats"])
                ops.done(op_key, stats, op_conn)
            except SystemExit:
                with run_lock:
                    run_state["stopped"] = True
                    run_state["error"] = "已手动停止"
                _log("运行已手动停止")
                ops.error(op_key, "stopped", op_conn)
            except Exception as e:  # noqa: BLE001 - surface to UI log
                with run_lock:
                    run_state["error"] = str(e)
                _log(f"运行出错：{e}")
                ops.error(op_key, str(e), op_conn)
            finally:
                with run_lock:
                    run_state["running"] = False
                    run_state["finished_at"] = datetime.now().isoformat(
                        timespec="seconds")

        threading.Thread(target=worker, daemon=True).start()
        return {"started": True, "running": True}

    @app.post("/run/pause")
    def run_pause():
        with run_lock:
            if not run_state["running"]:
                return {"paused": False, "error": "没有运行中的任务"}
            run_state["paused"] = True
        return {"paused": True}

    @app.post("/run/resume")
    def run_resume():
        with run_lock:
            if not run_state["running"]:
                return {"resumed": False, "error": "没有运行中的任务"}
            run_state["paused"] = False
        return {"resumed": True}

    @app.post("/run/stop")
    def run_stop():
        with run_lock:
            if not run_state["running"]:
                return {"stopped": False, "error": "没有运行中的任务"}
            run_state["stop_requested"] = True
            run_state["paused"] = False  # unblock pausable_log so it can exit
        return {"stopped": True}

    @app.get("/api/run-status")
    def api_run_status():
        with run_lock:
            return {
                "running": run_state["running"],
                "started_at": run_state["started_at"],
                "finished_at": run_state["finished_at"],
                "stats": dict(run_state["stats"]),
                "logs": list(run_state["logs"]),
                "error": run_state["error"],
                "paused": run_state["paused"],
                "stopped": run_state["stopped"],
                "source_current": run_state["source_current"],
                "source_total": run_state["source_total"],
            }

    # ---- rewrite progress (per-article) ----
    rewrite_main_lock = threading.Lock()
    rewrite_states: dict[int, dict] = {}
    rewrite_locks: dict[int, threading.Lock] = {}

    def _rewrite_state(article_id: int) -> dict:
        with rewrite_main_lock:
            if article_id not in rewrite_states:
                rewrite_states[article_id] = {"running": False, "logs": [],
                                              "error": None, "done": False,
                                              "draft_id": None}
                rewrite_locks[article_id] = threading.Lock()
            return rewrite_states[article_id]

    def _rewrite_log(article_id: int, msg: str,
                     op_key: str = "", op_conn: sqlite3.Connection | None = None) -> None:
        st = _rewrite_state(article_id)
        line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
        with rewrite_locks[article_id]:
            st["logs"].append(line)
            if len(st["logs"]) > 300:
                del st["logs"][:-300]
        if op_key and op_conn:
            ops.log(op_key, line, op_conn)

    # ---- narration / explainer-video generation ----
    # Per-draft state tracking: different drafts can run concurrently.
    nar_main_lock = threading.Lock()
    nar_states: dict[int, dict] = {}
    nar_locks: dict[int, threading.Lock] = {}

    def _nar_state(draft_id: int) -> dict:
        with nar_main_lock:
            if draft_id not in nar_states:
                nar_states[draft_id] = {"running": False, "logs": [],
                                        "error": None, "done": False}
                nar_locks[draft_id] = threading.Lock()
            return nar_states[draft_id]

    def _nar_log(draft_id: int, msg: str) -> None:
        st = _nar_state(draft_id)
        line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
        with nar_locks[draft_id]:
            st["logs"].append(line)
            if len(st["logs"]) > 300:
                del st["logs"][:-300]

    @app.post("/drafts/{draft_id}/narration")
    def trigger_narration(draft_id: int, tts_provider: str | None = None,
                          tts_voice: str | None = None):
        err = LG.require(license_mgr, LF.VIDEO)
        if err:
            return err
        st = _nar_state(draft_id)
        lk = nar_locks[draft_id]
        with lk:
            if st["running"]:
                return {"started": False, "running": True,
                        "message": "该草稿已有生成任务在进行"}
            st.update(running=True, logs=[], error=None, done=False)

        def worker():
            try:
                run_config = _current_config(get_store())
                llm = _resolve_provider(run_config)
                tts = _build_tts(run_config, tts_provider, tts_voice)
                generate_narration(draft_id, get_store(), llm, tts, config,
                                   tts_provider=tts_provider,
                                   tts_voice=tts_voice,
                                   progress=lambda m: _nar_log(draft_id, m))
                with nar_locks[draft_id]:
                    st["done"] = True
            except Exception as e:  # noqa: BLE001 - surface to UI
                with nar_locks[draft_id]:
                    st["error"] = str(e)
                _nar_log(draft_id, f"生成出错：{e}")
            finally:
                with nar_locks[draft_id]:
                    st["running"] = False

        threading.Thread(target=worker, daemon=True).start()
        return {"started": True, "running": True}

    @app.get("/api/narration-status")
    def api_narration_status(draft_id: int | None = None):
        base = {"running": False, "logs": [], "error": None, "done": False}
        if draft_id is not None:
            st = _nar_state(draft_id)
            with nar_locks[draft_id]:
                base["running"] = st["running"]
                base["logs"] = list(st["logs"])
                base["error"] = st["error"]
                base["done"] = st["done"]
            if not base["running"]:
                base["script"] = load_narration(draft_id, config)
        return base

    # ---- media localization (download draft images/videos to local paths) ----
    # Per-draft state tracking, mirroring the narration flow above.
    loc_main_lock = threading.Lock()
    loc_states: dict[int, dict] = {}
    loc_locks: dict[int, threading.Lock] = {}

    def _loc_state(draft_id: int) -> dict:
        with loc_main_lock:
            if draft_id not in loc_states:
                loc_states[draft_id] = {"running": False, "logs": [],
                                        "error": None, "done": False,
                                        "result": None}
                loc_locks[draft_id] = threading.Lock()
            return loc_states[draft_id]

    def _loc_log(draft_id: int, msg: str) -> None:
        st = _loc_state(draft_id)
        line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
        with loc_locks[draft_id]:
            st["logs"].append(line)
            if len(st["logs"]) > 300:
                del st["logs"][:-300]

    @app.post("/api/draft/{draft_id}/localize")
    def api_draft_localize(draft_id: int, images: bool = Form(True),
                           videos: bool = Form(True)):
        st = _loc_state(draft_id)
        lk = loc_locks[draft_id]
        with lk:
            if st["running"]:
                return {"started": False, "running": True,
                        "message": "该草稿已有本地化任务在进行"}
            st.update(running=True, logs=[], error=None, done=False,
                      result=None, body_md=None)

        def worker():
            try:
                store = get_store()
                body = store.read_draft_body(draft_id)
                content = body.get("body_md", "") or ""
                imgs = content_images(content)
                vids = content_videos(content)
                _loc_log(draft_id, f"图片 {len(imgs)} 张，视频 {len(vids)} 个")
                art = Article(
                    title=body.get("title_cn") or "",
                    content_md=content,
                    url=body.get("source_url", "") or "",
                    source_name=body.get("source_name", "") or "",
                    source_type="scrape",
                    published_at=None,
                    images=list(imgs),
                    raw_summary=None,
                    fetched_at=datetime.now(timezone.utc),
                    videos=list(vids),
                )
                # relativize=False → keep absolute /media/... paths so the
                # in-app editor/preview renders the localized media directly.
                localize_article(art, config,
                                 progress=lambda m: _loc_log(draft_id, m),
                                 download_images=images,
                                 download_videos=videos,
                                 relativize=False)
                n_img = sum(1 for u in art.images if u.startswith("/media/"))
                n_vid = sum(1 for u in art.videos if u.startswith("/media/"))
                # Persist the rewritten body (local paths) back to the draft,
                # preserving other front-matter fields.
                titles = body.get("title_candidates", []) or []
                store.update_draft_body(
                    draft_id, title_candidates=titles,
                    body_md=art.content_md, status=None,
                    title_cn=body.get("title_cn"))
                _loc_log(draft_id,
                         f"完成：本地图片 {n_img} 张，本地视频 {n_vid} 个")
                with loc_locks[draft_id]:
                    st["result"] = {"images": n_img, "videos": n_vid}
                    st["body_md"] = art.content_md
                    st["done"] = True
            except Exception as e:  # noqa: BLE001 - surface to UI
                with loc_locks[draft_id]:
                    st["error"] = str(e)
                _loc_log(draft_id, f"本地化出错：{e}")
            finally:
                with loc_locks[draft_id]:
                    st["running"] = False

        threading.Thread(target=worker, daemon=True).start()
        return {"started": True, "running": True}

    @app.get("/api/draft/{draft_id}/localize-status")
    def api_draft_localize_status(draft_id: int):
        base = {"running": False, "logs": [], "error": None, "done": False,
                "result": None, "body_md": None}
        st = _loc_state(draft_id)
        with loc_locks[draft_id]:
            base["running"] = st["running"]
            base["logs"] = list(st["logs"])
            base["error"] = st["error"]
            base["done"] = st["done"]
            base["result"] = st["result"]
            base["body_md"] = st.get("body_md")
        return base

    @app.get("/api/script/{draft_id}")
    def api_get_script(draft_id: int):
        return load_narration(draft_id, config) or {
            "scenes": [], "images": [], "videos": []}

    @app.post("/api/script/{draft_id}")
    async def api_save_script(draft_id: int, request: Request):
        data = await request.json()
        script = save_scenes(draft_id, data.get("scenes", []), config)
        saved = script.get("scenes", [])
        # Log first scene's bg_custom for debugging
        if saved:
            s0 = saved[0]
            print(f"[save_script] scene[0] bg_custom={s0.get('bg_custom')!r}  media={s0.get('media')!r}", flush=True)
        return {"ok": True, "scenes": saved}

    @app.post("/api/script/{draft_id}/resynth")
    async def api_resynth_script(draft_id: int, request: Request):
        err = LG.require(license_mgr, LF.VIDEO)
        if err:
            return err
        data = await request.json()
        indices = [int(i) for i in (data.get("indices") or [])]
        tts_provider = data.get("tts_provider")
        tts_voice = data.get("tts_voice")
        # optionally persist edits sent alongside before re-synth
        if data.get("scenes"):
            save_scenes(draft_id, data["scenes"], config)
        st = _nar_state(draft_id)
        lk = nar_locks[draft_id]
        with lk:
            if st["running"]:
                return {"started": False, "running": True,
                        "message": "该草稿已有配音/生成任务在进行"}
            st.update(running=True, logs=[], error=None, done=False)

        def worker():
            try:
                run_config = _current_config(get_store())
                tts = _build_tts(run_config, tts_provider, tts_voice)
                resynth_scenes(draft_id, indices, tts, config,
                               tts_provider=tts_provider,
                               tts_voice=tts_voice,
                               progress=lambda m: _nar_log(draft_id, m))
                with nar_locks[draft_id]:
                    st["done"] = True
            except Exception as e:  # noqa: BLE001
                with nar_locks[draft_id]:
                    st["error"] = str(e)
                _nar_log(draft_id, f"出错：{e}")
            finally:
                with nar_locks[draft_id]:
                    st["running"] = False

        threading.Thread(target=worker, daemon=True).start()
        return {"started": True, "running": True}

    @app.post("/api/script/{draft_id}/upload-bg")
    async def api_upload_bg(draft_id: int, file: UploadFile = File(...)):
        """Upload a custom background image for a scene.

        Saves to *data/videos/draft-{id}/bg-{uuid}.ext* and returns the
        relative path the frontend can embed in a scene's *bg_custom* field.
        """
        import uuid
        from fastapi.responses import JSONResponse

        out_dir = config.videos_dir / f"draft-{draft_id}"
        out_dir.mkdir(parents=True, exist_ok=True)

        ext = (Path(file.filename or "image.jpg").suffix
               if file.filename else ".jpg")
        name = f"bg-{uuid.uuid4().hex[:12]}{ext}"
        dest = out_dir / name

        content = await file.read()
        dest.write_bytes(content)

        return {"ok": True, "path": name,
                "url": f"/videos/draft-{draft_id}/{name}"}

    @app.post("/api/script/{draft_id}/ai-bg")
    async def api_ai_bg(draft_id: int, prompt: str = Form(...)):
        """Generate a background image via AI for a given scene.

        Takes the prompt text (typically the scene's visual description),
        calls the configured image provider, saves the result to the video
        draft directory, and returns the relative path.
        """
        import uuid

        run_config = _current_config(get_store())
        out_dir = config.videos_dir / f"draft-{draft_id}"
        out_dir.mkdir(parents=True, exist_ok=True)

        name = f"bg-ai-{uuid.uuid4().hex[:12]}.jpg"
        dest = out_dir / name

        try:
            img_provider = get_image_provider(
                run_config.image_provider,
                api_key=run_config.image_api_key,
                model=run_config.image_model,
                base_url=run_config.image_api_base)
            img_provider.generate(prompt, dest)
            return {"ok": True, "path": name,
                    "url": f"/videos/draft-{draft_id}/{name}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.post("/api/script/{draft_id}/scrape-bg")
    def api_scrape_bg(draft_id: int, query: str = Form(...)):
        """Search web for images matching query, download the best match.

        Strategy:
        1) DuckDuckGo image search via `ddgs` library (no API key needed).
        2) Fallback: Wikipedia API (free, reliable, CC-licensed images).

        This is a sync endpoint to avoid asyncio compatibility issues with httpx.
        FastAPI runs sync endpoints in a threadpool, so it won't block the server.
        """
        import uuid
        import time
        import httpx
        from urllib.parse import quote

        _UA = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
        }

        out_dir = config.videos_dir / f"draft-{draft_id}"
        out_dir.mkdir(parents=True, exist_ok=True)

        def _try_download(img_url: str, referer: str = "") -> dict | None:
            """Try to download an image URL and save to disk."""
            try:
                hdrs = {"User-Agent": _UA["User-Agent"]}
                if referer:
                    hdrs["Referer"] = referer
                resp = httpx.get(img_url, timeout=15, follow_redirects=True,
                                 headers=hdrs)
                resp.raise_for_status()
                ext = Path(img_url).suffix.split("?")[0].lower() or ".jpg"
                if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                    ext = ".jpg"
                name = f"bg-web-{uuid.uuid4().hex[:12]}{ext}"
                dest = out_dir / name
                dest.write_bytes(resp.content)
                return {"ok": True, "path": name,
                        "url": f"/videos/draft-{draft_id}/{name}"}
            except Exception:
                return None

        # ---- Strategy 1: DuckDuckGo image search via ddgs ----
        try:
            from ddgs import DDGS
            results = list(DDGS().images(
                query,
                max_results=15,
            ))
            for img in results:
                img_url = img.get("image") or img.get("thumbnail")
                if not img_url:
                    continue
                result = _try_download(img_url, referer=img.get("url", ""))
                if result:
                    return result
        except ImportError:
            pass  # ddgs not installed, skip
        except Exception:
            pass  # ddgs failed, fall through to Wikipedia

        # ---- Strategy 2: Wikipedia API (fallback) ----
        for lang in ("en", "zh"):
            try:
                r = httpx.get(
                    f"https://{lang}.wikipedia.org/w/api.php",
                    params={"action": "query", "list": "search",
                            "srsearch": query, "format": "json",
                            "srlimit": 3},
                    timeout=10, headers=_UA, follow_redirects=True)
                data = r.json()
                pages = data.get("query", {}).get("search", [])
                for page in pages:
                    title = page["title"]
                    r2 = httpx.get(
                        f"https://{lang}.wikipedia.org/w/api.php",
                        params={"action": "query", "titles": title,
                                "prop": "pageimages", "format": "json",
                                "pithumbsize": 800},
                        timeout=10, headers=_UA, follow_redirects=True)
                    data2 = r2.json()
                    for pid, info in data2.get("query", {}).get(
                            "pages", {}).items():
                        if pid == "-1":
                            continue
                        thumb = info.get("thumbnail", {}).get("source")
                        if thumb:
                            time.sleep(0.5)  # avoid Wikimedia rate limit
                            result = _try_download(
                                thumb,
                                referer=f"https://{lang}.wikipedia.org/")
                            if result:
                                return result
            except Exception:
                continue

        return {"ok": False, "error": "未搜到可下载的图片，请尝试修改画面描述"}

    # ---- available TTS voices for the per-draft voice selector ----
    _KITTEN_PROFILES = [
        {"id": "assistant", "label": "助手（默认）"},
        {"id": "female", "label": "女声"},
        {"id": "female_warm", "label": "温柔女声"},
        {"id": "male", "label": "男声"},
        {"id": "male_deep", "label": "低沉男声"},
        {"id": "child", "label": "儿童声"},
    ]
    _OPENAI_VOICES = [
        {"id": "alloy", "label": "Alloy"},
        {"id": "echo", "label": "Echo"},
        {"id": "fable", "label": "Fable"},
        {"id": "onyx", "label": "Onyx"},
        {"id": "nova", "label": "Nova"},
        {"id": "shimmer", "label": "Shimmer"},
    ]

    @app.get("/api/voices")
    def api_list_voices(provider: str = ""):
        """Return available voice options for the given TTS provider.

        If *provider* is empty, fall back to the configured default.
        """
        from app.tts.voices import list_voices
        provider = (provider or config.tts_provider or "kitten").lower()
        cloned = list_voices(config)
        cloned_fmt = [{"id": v["id"], "label": v.get("name", v["id"])}
                      for v in cloned]
        voices_map: dict[str, list[dict]] = {
            "kitten": list(_KITTEN_PROFILES),
            "openai": list(_OPENAI_VOICES),
            "cosyvoice": cloned_fmt,
        }
        return {"voices": voices_map.get(provider, [])}

    # ---- localize a single article's media (before transcribing) ----
    mlz_lock = threading.Lock()
    mlz_state = {"running": False, "article_id": None, "logs": [],
                 "error": None, "done": False, "stats": {}}

    def _mlz_log(msg):
        line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
        with mlz_lock:
            mlz_state["logs"].append(line)
            if len(mlz_state["logs"]) > 500:
                del mlz_state["logs"][:-500]

    @app.post("/archive/{article_id}/localize")
    def trigger_localize(article_id: int):
        with mlz_lock:
            if mlz_state["running"]:
                return {"started": False, "running": True,
                        "message": "已有本地化任务在进行"}
            mlz_state.update(running=True, article_id=article_id, logs=[],
                             error=None, done=False, stats={})

        def worker():
            op_key = f"localize-{article_id}"
            op_conn = _db_conn()
            ops.begin(op_key, "localize", article_id, op_conn)
            try:
                stats = localize_one(article_id, get_store(), config,
                                     download_images=config.download_images,
                                     download_videos=config.download_videos,
                                     progress=_mlz_log)
                with mlz_lock:
                    mlz_state["stats"] = stats
                    mlz_state["done"] = True
                ops.done(op_key, stats, op_conn)
            except Exception as e:  # noqa: BLE001
                with mlz_lock:
                    mlz_state["error"] = str(e)
                _mlz_log(f"出错：{e}")
                ops.error(op_key, str(e), op_conn)
            finally:
                with mlz_lock:
                    mlz_state["running"] = False

        threading.Thread(target=worker, daemon=True).start()
        return {"started": True, "running": True}

    @app.get("/api/localize-status")
    def api_localize_status():
        with mlz_lock:
            return {
                "running": mlz_state["running"],
                "article_id": mlz_state["article_id"],
                "logs": list(mlz_state["logs"]),
                "error": mlz_state["error"],
                "done": mlz_state["done"],
                "stats": dict(mlz_state["stats"]),
            }

    # ---- explainer video composition (ffmpeg) ----
    vid_lock = threading.Lock()
    vid_state = {"running": False, "draft_id": None, "logs": [],
                 "error": None, "done": False}

    def _vid_log(msg):
        line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
        with vid_lock:
            vid_state["logs"].append(line)
            if len(vid_state["logs"]) > 300:
                del vid_state["logs"][:-300]

    @app.post("/drafts/{draft_id}/video")
    def trigger_video(draft_id: int):
        err = LG.require(license_mgr, LF.VIDEO)
        if err:
            return err
        with vid_lock:
            if vid_state["running"]:
                return {"started": False, "running": True,
                        "message": "已有合成任务在进行"}
            vid_state.update(running=True, draft_id=draft_id, logs=[],
                             error=None, done=False)

        def worker():
            try:
                rc = _current_config(get_store())
                # Debug: peek at script.json before building
                import json as _json
                _sp = rc.videos_dir / f"draft-{draft_id}" / "script.json"
                if _sp.exists():
                    _s = _json.loads(_sp.read_text("utf-8"))
                    for _si, _sc in enumerate(_s.get("scenes", [])):
                        if _sc.get("bg_custom"):
                            print(f"[build_video] scene[{_si}] bg_custom={_sc['bg_custom']!r}  exists={(_sp.parent / _sc['bg_custom']).exists()}", flush=True)
                build_explainer_video(draft_id, rc, progress=_vid_log)
                with vid_lock:
                    vid_state["done"] = True
            except Exception as e:  # noqa: BLE001 - surface to UI
                with vid_lock:
                    vid_state["error"] = str(e)
                _vid_log(f"合成出错：{e}")
            finally:
                with vid_lock:
                    vid_state["running"] = False

        threading.Thread(target=worker, daemon=True).start()
        return {"started": True, "running": True}

    @app.get("/api/video-status")
    def api_video_status(draft_id: int | None = None):
        with vid_lock:
            base = {
                "running": vid_state["running"],
                "draft_id": vid_state["draft_id"],
                "logs": list(vid_state["logs"]),
                "error": vid_state["error"],
                "done": vid_state["done"],
            }
        if draft_id is not None:
            base["has_video"] = video_path(draft_id, config) is not None
        return base

    @app.post("/voices")
    async def voices_add(file: UploadFile = File(...), name: str = Form("")):
        err = LG.require(license_mgr, LF.VOICE_CLONE)
        if err:
            return err
        data = await file.read()
        if not data:
            return RedirectResponse(url="/settings?import_error=1", status_code=303)
        try:
            add_voice(config, name or file.filename or "声音",
                      data, file.filename or "sample.wav")
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return RedirectResponse(url="/settings", status_code=303)

    @app.post("/voices/{voice_id}/delete")
    def voices_delete(voice_id: str):
        delete_voice(config, voice_id)
        return RedirectResponse(url="/settings", status_code=303)

    @app.post("/api/voices/record")
    async def voices_record(blob: UploadFile = File(...), name: str = Form("")):
        """Receive a browser-recorded blob and save it as a new cloned voice."""
        err = LG.require(license_mgr, LF.VOICE_CLONE)
        if err:
            return err
        data = await blob.read()
        if not data:
            return JSONResponse({"error": "empty recording"}, status_code=400)
        try:
            entry = add_voice(config, name, data,
                              blob.filename or "recording.webm")
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse({"voice": entry})

    @app.get("/voices-audio/{voice_id}")
    def voices_audio(voice_id: str):
        p = voice_sample_path(config, voice_id)
        if not p:
            return HTMLResponse("not found", status_code=404)
        return FileResponse(str(p))

    # ---- clear data ----

    @app.post("/clear/articles")
    def clear_articles():
        store = get_store()
        n = store.clear_articles()
        return RedirectResponse(url="/archive?cleared=" + str(n), status_code=303)

    @app.post("/clear/drafts")
    def clear_drafts():
        store = get_store()
        n = store.clear_drafts()
        return RedirectResponse(url="/drafts?cleared=" + str(n), status_code=303)

    @app.post("/clear/runs")
    def clear_runs():
        store = get_store()
        n = store.clear_runs()
        return RedirectResponse(url="/?cleared_runs=" + str(n), status_code=303)

    @app.post("/drafts/{draft_id}/adapt")
    def draft_adapt(draft_id: int, platform: str = Form(...)):
        err = LG.require(license_mgr, LF.PLATFORM_SYNC)
        if err:
            return err
        store = get_store()
        meta = store.read_draft_body(draft_id)
        if not meta:
            return JSONResponse({"error": "draft not found"}, status_code=404)
        titles = meta.get("title_candidates") or ["稿件"]
        run_config = _current_config(store)
        provider = _resolve_provider(run_config)
        result = adapt(meta.get("body_md", ""), titles[0], platform, provider)

        # Append promotion footer if configured
        footer = run_config.promotion_footer
        if footer:
            result["body_md"] = result["body_md"].rstrip() + f"\n\n---\n{footer}\n"

        p = get_platform(platform)
        return JSONResponse({
            "title_candidates": result["title_candidates"],
            "body_md": result["body_md"],
            "publish_url": p.publish_url if p else None,
            "platform_label": p.label if p else platform,
        })

    @app.post("/drafts/{draft_id}/cover-generate")
    def draft_cover_generate(draft_id: int):
        """AI-generate a cover image for the draft."""
        store = get_store()
        meta = store.read_draft_body(draft_id)
        if not meta:
            return JSONResponse({"ok": False, "error": "草稿不存在"}, status_code=404)
        run_config = _current_config(store)
        provider = get_image_provider(
            run_config.image_provider,
            run_config.image_api_key or run_config.llm_api_key,
            model=run_config.image_model,
            base_url=run_config.image_api_base or run_config.llm_api_base)
        title = (meta.get("title_cn")
                 or (meta.get("title_candidates") or [""])[0]
                 or "cover")
        name = f"cover-{_slugify(title)[:40] or 'cover'}.png"
        out = run_config.images_dir / name
        try:
            provider.generate(
                prompt=f"科技自媒体封面图：{title}", out_path=out)
            store.set_draft_cover(draft_id, name)
            return {"ok": True, "cover_image": name}
        except Exception as exc:
            return {"ok": False, "error": f"封面生成失败：{exc}"}

    @app.post("/drafts/{draft_id}/cover-scrape")
    def draft_cover_scrape(draft_id: int):
        """Search web for a cover image using the article title."""
        store = get_store()
        meta = store.read_draft_body(draft_id)
        if not meta:
            return JSONResponse({"ok": False, "error": "草稿不存在"}, status_code=404)
        title = (meta.get("title_cn")
                 or (meta.get("title_candidates") or [""])[0]
                 or "")
        if not title:
            return {"ok": False, "error": "没有可用的标题用于搜索"}
        run_config = _current_config(store)
        try:
            from ddgs import DDGS
            results = list(DDGS().images(title, max_results=5))
        except ImportError:
            return {"ok": False, "error": "ddgs 未安装"}
        except Exception as exc:
            return {"ok": False, "error": f"搜索失败：{exc}"}

        import uuid
        import httpx
        for img in results:
            img_url = img.get("image") or img.get("thumbnail")
            if not img_url:
                continue
            try:
                resp = httpx.get(img_url, timeout=15, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                ext = Path(img_url).suffix.split("?")[0].lower() or ".jpg"
                if ext not in (".jpg", ".jpeg", ".png"):
                    ext = ".jpg"
                name = f"cover-web-{uuid.uuid4().hex[:8]}{ext}"
                dest = run_config.images_dir / name
                dest.write_bytes(resp.content)
                store.set_draft_cover(draft_id, name)
                return {"ok": True, "cover_image": name}
            except Exception:
                continue
        return {"ok": False, "error": "未搜到可下载的封面图片"}

    @app.post("/drafts/{draft_id}/cover-clear")
    def draft_cover_clear(draft_id: int):
        """Clear the custom cover image, reverting to the system default."""
        store = get_store()
        store.set_draft_cover(draft_id, None)
        return {"ok": True}

    def _localize_before_publish(draft_id: int, meta: dict, run_config,
                                 store) -> dict:
        """Download remote images needed by the platform before publishing.

        Body videos are deliberately not downloaded here: WeChat 图文 does not
        accept external iframe players, and the renderer preserves them as
        source links. Downloading full videos only to discard their embeds made
        draft publishing unnecessarily slow.

        Rewrites image URLs to /media/ paths, persists the result, and returns
        fresh metadata.
        Best-effort: on any failure the original meta is returned so the push
        can still proceed."""
        content = meta.get("body_md", "") or ""
        if not content:
            return meta
        try:
            art = Article(
                title=meta.get("title_cn") or "", content_md=content,
                url=meta.get("source_url", "") or "",
                source_name=meta.get("source_name", "") or "",
                source_type="scrape", published_at=None,
                images=list(content_images(content)), raw_summary=None,
                fetched_at=datetime.now(timezone.utc),
                videos=list(content_videos(content)))
            localize_article(art, run_config,
                             progress=lambda m: logger.info("[publish-localize] %s", m),
                             download_images=True, download_videos=False,
                             relativize=False)
            if art.content_md and art.content_md != content:
                store.update_draft_body(
                    draft_id,
                    title_candidates=meta.get("title_candidates", []) or [],
                    body_md=art.content_md, status=None,
                    title_cn=meta.get("title_cn"))
                return store.read_draft_body(draft_id) or meta
        except Exception as exc:  # noqa: BLE001 - never block publish
            logger.warning("[publish-localize] failed (continuing): %s", exc)
        return meta

    # WeChat publishing includes media localization/uploads and, for direct
    # publish, an asynchronous WeChat review task.  Keep one workflow per
    # draft so clients can poll through the true terminal state and retries
    # cannot create duplicate drafts/publish jobs.
    wechat_publish_main_lock = threading.Lock()
    wechat_publish_states: dict[int, dict] = {}
    wechat_publish_locks: dict[int, threading.Lock] = {}

    def _wechat_publish_state(draft_id: int) -> dict:
        with wechat_publish_main_lock:
            if draft_id not in wechat_publish_states:
                wechat_publish_states[draft_id] = {
                    "running": False, "done": False, "ok": False,
                    "mode": None, "error": None, "result": None,
                }
                wechat_publish_locks[draft_id] = threading.Lock()
            return wechat_publish_states[draft_id]

    @app.post("/drafts/{draft_id}/publish/wechat")
    def draft_publish_wechat(draft_id: int, mode: str = Form("draft"),
                             kind: str = Form("article"),
                             theme: str = Form("default")):
        """One-click publish a draft to a WeChat Official Account.

        kind=article: 图文 -> mode "draft" (草稿箱) or "publish" (草稿+发布).
        kind=video:   upload the generated mp4 to 素材库.
        theme: visual formatting theme for the 图文.
        """
        err = LG.require(license_mgr, LF.PLATFORM_SYNC)
        if err:
            return err
        if mode not in ("draft", "publish"):
            return JSONResponse(
                {"ok": False, "error": f"不支持的微信发布模式：{mode}"},
                status_code=400)
        if kind not in ("article", "video"):
            return JSONResponse(
                {"ok": False, "error": f"不支持的微信内容类型：{kind}"},
                status_code=400)
        store = get_store()
        meta = store.read_draft_body(draft_id)
        if not meta:
            return JSONResponse(
                {"ok": False, "error": "draft not found"}, status_code=404)
        run_config = _current_config(store)
        from app.wechat import get_wechat_client
        client = get_wechat_client(run_config)
        if client is None:
            return JSONResponse(
                {"ok": False,
                 "error": "未配置公众号 AppID/AppSecret，请到「设置」页填写。"},
                status_code=400)

        state = _wechat_publish_state(draft_id)
        lock = wechat_publish_locks[draft_id]
        with lock:
            if state["running"]:
                return JSONResponse({
                    "ok": True, "running": True, "done": False,
                    "mode": state["mode"], "already_running": True,
                }, status_code=202)
            state.update(running=True, done=False, ok=False, mode=mode,
                         error=None, result=None)

        def worker():
            from app.wechat import WeChatError
            from app.wechat.publish import (
                publish_article, upload_video, wait_for_publish)
            worker_store = get_store()
            try:
                worker_meta = worker_store.read_draft_body(draft_id)
                if not worker_meta:
                    raise RuntimeError("draft not found")
                if kind == "video":
                    result = upload_video(
                        client, run_config, draft_id, worker_meta)
                else:
                    # This localization and every media upload are part of the
                    # tracked workflow; the UI remains pending throughout.
                    worker_meta = _localize_before_publish(
                        draft_id, worker_meta, run_config, worker_store)
                    result = publish_article(
                        client, run_config, worker_meta, mode=mode, theme=theme)
                    if result.get("ok") and mode == "publish":
                        publish_id = result.get("publish_id")
                        if not publish_id:
                            raise RuntimeError(
                                "微信已接收发布请求，但未返回 publish_id")
                        result["publish_detail"] = wait_for_publish(
                            client, str(publish_id), timeout=1800.0)
                if not result.get("ok"):
                    raise RuntimeError(result.get("error") or "微信推送失败")
                if result.get("mode") == "publish":
                    worker_store.set_draft_status(draft_id, "published")
                with lock:
                    state.update(ok=True, result=result)
            except WeChatError as exc:
                with lock:
                    state["error"] = (
                        f"微信接口错误 {exc.errcode}: {exc.errmsg}")
            except Exception as exc:  # noqa: BLE001 - surface exact failure
                with lock:
                    state["error"] = str(exc)
            finally:
                with lock:
                    state.update(running=False, done=True)

        threading.Thread(target=worker, daemon=True).start()
        return JSONResponse(
            {"ok": True, "running": True, "done": False, "mode": mode},
            status_code=202)

    @app.get("/api/draft/{draft_id}/publish/wechat/status")
    def draft_publish_wechat_status(draft_id: int):
        state = _wechat_publish_state(draft_id)
        lock = wechat_publish_locks[draft_id]
        with lock:
            return {
                "running": state["running"], "done": state["done"],
                "ok": state["ok"], "mode": state["mode"],
                "error": state["error"], "result": state["result"],
            }

    @app.post("/drafts/{draft_id}/styled-html")
    def draft_styled_html(draft_id: int, platform: str = Form("wechat"),
                          theme: str = Form("default")):
        """Return themed, inline-CSS HTML (spider-media style) for a draft,
        with local images base64-embedded so it can be pasted straight into the
        平台 editor (used for 头条, and as a manual fallback for 公众号)."""
        err = LG.require(license_mgr, LF.PLATFORM_SYNC)
        if err:
            return err
        store = get_store()
        meta = store.read_draft_body(draft_id)
        if not meta:
            return JSONResponse({"ok": False, "error": "draft not found"},
                                status_code=404)
        run_config = _current_config(store)
        from app.wechat.formatter import render_styled_html, list_themes
        from app.wechat.publish import (
            inline_images_base64, _absolutize_body_md)
        titles = meta.get("title_candidates") or []
        title = (meta.get("title_cn") or (titles[0] if titles else "")).strip()
        body_md = _absolutize_body_md(meta.get("body_md", ""))
        html = render_styled_html(body_md, platform=platform, theme=theme)
        html = inline_images_base64(html, run_config, platform=platform)
        return JSONResponse({"ok": True, "title": title, "html": html,
                             "platform": platform, "theme": theme,
                             "themes": list_themes(platform)})

    @app.post("/drafts/{draft_id}/wechat-channels/prepare")
    def draft_channels_prepare(draft_id: int):
        """Prepare a half-automatic 视频号 (Channels) publish.

        WeChat has no server API to post a video to 视频号, so we only generate
        the caption + locate the video file; the UI copies the caption and
        opens 视频号助手 for the user to drag in the file and paste.
        """
        err = LG.require(license_mgr, LF.PLATFORM_SYNC)
        if err:
            return err
        store = get_store()
        meta = store.read_draft_body(draft_id)
        if not meta:
            return JSONResponse({"ok": False, "error": "draft not found"},
                                status_code=404)
        run_config = _current_config(store)
        provider = _resolve_provider(run_config)
        from app.wechat.channels import (
            build_channels_caption, CHANNELS_CREATE_URL)
        cap = build_channels_caption(meta, provider)
        mp4 = video_path(draft_id, config)
        has_video = mp4 is not None
        return JSONResponse({
            "ok": True,
            "title": cap["title"],
            "description": cap["description"],
            "hashtags": cap["hashtags"],
            "caption": cap["caption"],
            "create_url": CHANNELS_CREATE_URL,
            "has_video": has_video,
            "video_url": f"/videos/draft-{draft_id}/video.mp4" if has_video else None,
            "video_local_path": str(mp4) if has_video else None,
        })

    @app.post("/drafts/{draft_id}/video-prepare")
    def draft_video_prepare(draft_id: int, platform: str = Form(...)):
        """Half-automatic video publish for any registered platform that
        exposes a video-publish page (e.g. 头条). Generates a platform-styled
        caption + locates the video; the UI copies the caption, opens the
        creator page, and offers the video file. No account automation.
        """
        err = LG.require(license_mgr, LF.PLATFORM_SYNC)
        if err:
            return err
        store = get_store()
        meta = store.read_draft_body(draft_id)
        if not meta:
            return JSONResponse({"ok": False, "error": "draft not found"},
                                status_code=404)
        p = get_platform(platform)
        if p is None:
            return JSONResponse({"ok": False, "error": "未知平台"},
                                status_code=404)
        video_url_page = getattr(p, "video_publish_url", None)
        if not video_url_page:
            return JSONResponse(
                {"ok": False, "error": f"{p.label} 暂不支持视频半自动发布"},
                status_code=400)
        run_config = _current_config(store)
        provider = _resolve_provider(run_config)
        from app.platforms.video_prepare import build_video_caption
        cap = build_video_caption(meta, provider, p.video_caption_style())
        mp4 = video_path(draft_id, config)
        has_video = mp4 is not None
        return JSONResponse({
            "ok": True,
            "platform_label": p.label,
            "title": cap["title"],
            "description": cap["description"],
            "hashtags": cap["hashtags"],
            "caption": cap["caption"],
            "create_url": video_url_page,
            "has_video": has_video,
            "video_url": f"/videos/draft-{draft_id}/video.mp4" if has_video else None,
            "video_local_path": str(mp4) if has_video else None,
        })

    def _voice_display_name(voice_id: str, config) -> str:
        """Resolve a voice ID to its display name, falling back to the raw value."""
        if not voice_id:
            return ""
        try:
            from app.tts.voices import list_voices
            for v in list_voices(config):
                if v["id"] == voice_id:
                    return v.get("name", voice_id)
        except Exception:                          # noqa: BLE001
            pass
        return voice_id

    @app.get("/api/license/status")
    def api_license_status():
        return JSONResponse(license_mgr.status_dict())

    @app.post("/api/license/activate")
    def api_license_activate(key: str = Form("")):
        key = (key or "").strip()
        if not key:
            return JSONResponse({"ok": False, "error": "请输入激活码"},
                                status_code=400)
        ok, msg = license_mgr.activate(key)
        return JSONResponse({"ok": ok, "message": msg,
                             "status": license_mgr.status_dict()},
                            status_code=200 if ok else 400)

    @app.post("/api/license/deactivate")
    def api_license_deactivate():
        license_mgr.deactivate()
        return JSONResponse({"ok": True, "status": license_mgr.status_dict()})

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page():
        return _serve_spa()

    @app.get("/api/settings")
    def api_settings():
        store = get_store()

        def _mask(v):
            if not (v and v.strip()):
                return {"set": False, "masked": ""}
            m = v[:4] + "..." + v[-4:] if len(v) > 8 else "****"
            return {"set": True, "masked": m}

        def _get(k):
            return store.get_setting(k) or ""

        return {
            "llm_provider": _get("llm_provider") or config.llm_provider,
            "llm_model": _get("llm_model") or (config.llm_model or ""),
            "llm_api_base": _get("llm_api_base") or (config.llm_api_base or ""),
            "llm_timeout": _get("llm_timeout") or (str(config.llm_timeout) if config.llm_timeout else "120"),
            "image_provider": _get("image_provider") or (config.image_provider or "mock"),
            "image_api_base": _get("image_api_base") or (config.image_api_base or ""),
            "image_model": _get("image_model") or (config.image_model or ""),
            "tts_provider": _get("tts_provider") or (config.tts_provider or "kitten"),
            "tts_api_base": _get("tts_api_base") or (config.tts_api_base or ""),
            "tts_model": _get("tts_model") or (config.tts_model or ""),
            "tts_voice": _get("tts_voice") or (config.tts_voice or ""),
            "tts_rate": _get("tts_rate") or (config.tts_rate or ""),
            "tts_pitch": _get("tts_pitch") or (config.tts_pitch or ""),
            "tts_instruct": _get("tts_instruct") or (config.tts_instruct or ""),
            "voices": list_voices(config),
            "data_dir": str(config.data_dir),
            "max_age_days": _get("max_age_days") or (str(config.max_age_days) if config.max_age_days else ""),
            "max_per_source": _get("max_per_source") or (str(config.max_per_source) if config.max_per_source else ""),
            "max_drafts": _get("max_drafts") or (str(config.max_drafts) if config.max_drafts else ""),
            "download_workers": _get("download_workers") or (str(config.download_workers) if config.download_workers else ""),
            "default_workers": os.cpu_count() or 4,
            "video_fit": _get("video_fit") or (config.video_fit or "fit"),
            "video_brand_name": _get("video_brand_name") or (config.video_brand_name or "Media Agent"),
            "avatar_enabled": (store.get_setting("avatar_enabled") or "0") not in ("0", "false", "no", ""),
            "avatar_image": _get("avatar_image") or (config.avatar_image or ""),
            "avatar_position": _get("avatar_position") or (config.avatar_position or "pip"),
            "avatar_provider": _get("avatar_provider") or (config.avatar_provider or "sadtalker"),
            "sadtalker_dir": _get("sadtalker_dir") or (config.sadtalker_dir or ""),
            "sadtalker_python": _get("sadtalker_python") or (config.sadtalker_python or ""),
            "sensitive_level": _get("sensitive_level") or (config.sensitive_level or "standard"),
            "sensitive_words": _get("sensitive_words") or (config.sensitive_words or ""),
            "promotion_footer": _get("promotion_footer") or (config.promotion_footer or ""),
            "seo_tags_enabled": (
                store.get_setting("seo_tags_enabled") or "1"
            ) not in ("0", "false", "no", ""),
            "cli_tool": _get("cli_tool") or (config.cli_tool or "auto"),
            "rewrite_priority": _get("rewrite_priority") or config.rewrite_priority,
            "fetch_proxy": _get("fetch_proxy") or (config.fetch_proxy or ""),
            "github_mirror": _get("github_mirror") or (config.github_mirror or ""),
            "huggingface_mirror": _get("huggingface_mirror") or (config.huggingface_mirror or ""),
            "download_images": (store.get_setting("download_images") or "1") not in ("0", "false", "no", ""),
            "download_videos": (store.get_setting("download_videos") or "1") not in ("0", "false", "no", ""),
            "relevance_filter": (store.get_setting("relevance_filter") or "1") not in ("0", "false", "no", ""),
            "wechat_appid": _get("wechat_appid") or (config.wechat_appid or ""),
            "wechat_author": _get("wechat_author") or (config.wechat_author or ""),
            "schedule_cron": store.get_setting("schedule_cron", ""),
            "schedule_enabled": store.get_setting("schedule_enabled", "0") == "1",
            "llm_api_key": _mask(store.get_setting("llm_api_key")),
            "image_api_key": _mask(store.get_setting("image_api_key")),
            "tts_api_key": _mask(store.get_setting("tts_api_key")),
            "wechat_appsecret": _mask(store.get_setting("wechat_appsecret")),
            "rewrite_styles": [s.to_public() for s in rewrite_styles.all_styles(store)],
            "rewrite_style": rewrite_styles.get_default_style_id(store),
            "license": license_mgr.status_dict(),
            "license_labels": LF.LABELS,
        }

    @app.post("/settings")
    def settings_save(llm_provider: str = Form(""),
                      llm_api_key: str = Form(""),
                      llm_model: str = Form(""),
                      llm_api_base: str = Form(""),
                      llm_timeout: str = Form(""),
                      image_provider: str = Form(""),
                      image_api_key: str = Form(""),
                      image_api_base: str = Form(""),
                      image_model: str = Form(""),
                      tts_provider: str = Form(""),
                      tts_api_key: str = Form(""),
                      tts_api_base: str = Form(""),
                      tts_model: str = Form(""),
                      tts_voice: str = Form(""),
                      tts_rate: str = Form(""),
                      tts_pitch: str = Form(""),
                      tts_instruct: str = Form(""),
                      max_age_days: str = Form(""),
                      max_per_source: str = Form(""),
                      max_drafts: str = Form(""),
                      download_workers: str = Form(""),
                        video_fit: str = Form(""),
                        video_brand_name: str = Form(""),
                        avatar_enabled: str = Form("0"),
                        avatar_image: str = Form(""),
                        avatar_position: str = Form(""),
                        avatar_provider: str = Form(""),

                       sensitive_level: str = Form(""),
                       sensitive_words: str = Form(""),
                       promotion_footer: str = Form(""),
                       seo_tags_enabled: str = Form("0"),
                       wechat_appid: str = Form(""),
                       wechat_appsecret: str = Form(""),
                       wechat_author: str = Form(""),
                       download_images: str = Form("0"),
                       download_videos: str = Form("0"),
                       relevance_filter: str = Form("0"),
                        cli_tool: str = Form(""),
                        rewrite_priority: str = Form(""),
                          fetch_proxy: str = Form(""),
                          github_mirror: str = Form(""),
                          huggingface_mirror: str = Form(""),
                        rewrite_style: str = Form(""),
                        schedule_cron: str = Form(""),
                       schedule_enabled: str = Form("0")):
        store = get_store()

        # String config fields: save if non-empty, delete if empty
        str_fields = {
            "llm_provider": llm_provider.strip(),
            "llm_model": llm_model.strip(),
            "llm_api_base": llm_api_base.strip(),
            "image_provider": image_provider.strip(),
            "image_api_base": image_api_base.strip(),
            "image_model": image_model.strip(),
            "tts_provider": tts_provider.strip(),
            "tts_api_base": tts_api_base.strip(),
            "tts_model": tts_model.strip(),
            "tts_voice": tts_voice.strip(),
            "tts_rate": tts_rate.strip(),
            "tts_pitch": tts_pitch.strip(),
            "tts_instruct": tts_instruct.strip(),
            "video_fit": video_fit.strip(),
            "video_brand_name": video_brand_name.strip(),
            "avatar_image": avatar_image.strip(),
            "avatar_position": avatar_position.strip(),
            "avatar_provider": avatar_provider.strip(),

            "sensitive_level": sensitive_level.strip(),
            "sensitive_words": sensitive_words.strip(),
            "promotion_footer": promotion_footer.strip(),
            "wechat_appid": wechat_appid.strip(),
            "wechat_author": wechat_author.strip(),
            "cli_tool": cli_tool.strip(),
            "rewrite_priority": rewrite_priority.strip(),
            "fetch_proxy": fetch_proxy.strip(),
            "github_mirror": github_mirror.strip(),
            "huggingface_mirror": huggingface_mirror.strip(),
        }
        for db_key, value in str_fields.items():
            if value:
                store.set_setting(db_key, value)
            else:
                store.delete_setting(db_key)

        # API key: only save if explicitly provided (non-empty)
        api_key = llm_api_key.strip()
        if api_key:
            store.set_setting("llm_api_key", api_key)
        # If empty, leave existing value unchanged

        # Image API key: only save if explicitly provided
        img_api_key = image_api_key.strip()
        if img_api_key:
            store.set_setting("image_api_key", img_api_key)

        # TTS API key: only save if explicitly provided
        tts_api_key = tts_api_key.strip()
        if tts_api_key:
            store.set_setting("tts_api_key", tts_api_key)

        # WeChat AppSecret: only save if explicitly provided (secret)
        wx_secret = wechat_appsecret.strip()
        if wx_secret:
            store.set_setting("wechat_appsecret", wx_secret)

        # Integer fields: only save if explicitly provided
        if llm_timeout.strip():
            try:
                int_val = int(llm_timeout.strip())
                if int_val > 0:
                    store.set_setting("llm_timeout", llm_timeout.strip())
                else:
                    store.delete_setting("llm_timeout")
            except ValueError:
                pass

        if max_age_days.strip():
            try:
                int_val = int(max_age_days.strip())
                if int_val >= 0:
                    store.set_setting("max_age_days", max_age_days.strip())
                else:
                    store.delete_setting("max_age_days")
            except ValueError:
                pass  # leave existing value unchanged
        # If empty, leave existing value unchanged

        if max_per_source.strip():
            try:
                int_val = int(max_per_source.strip())
                if int_val >= 0:
                    store.set_setting("max_per_source", max_per_source.strip())
                else:
                    store.delete_setting("max_per_source")
            except ValueError:
                pass
        # If empty, leave existing value unchanged

        if max_drafts.strip():
            try:
                int_val = int(max_drafts.strip())
                if int_val > 0:
                    store.set_setting("max_drafts", max_drafts.strip())
                else:
                    store.delete_setting("max_drafts")
            except ValueError:
                pass
        # If empty, leave existing value unchanged

        if download_workers.strip():
            try:
                int_val = int(download_workers.strip())
                if int_val > 0:
                    store.set_setting("download_workers", download_workers.strip())
                else:
                    store.delete_setting("download_workers")
            except ValueError:
                pass

        # Media localization options
        store.set_setting("download_images",
                          "1" if download_images in ("1", "on", "true") else "0")
        store.set_setting("download_videos",
                          "1" if download_videos in ("1", "on", "true") else "0")
        store.set_setting("relevance_filter",
                          "1" if relevance_filter in ("1", "on", "true") else "0")
        store.set_setting("avatar_enabled",
                          "1" if avatar_enabled in ("1", "on", "true") else "0")
        store.set_setting(
            "seo_tags_enabled",
            "1" if seo_tags_enabled in ("1", "on", "true") else "0")

        # Default rewrite style: validate against the registry; empty or the
        # builtin default clears the setting (falls back to deep-tech).
        try:
            rewrite_styles.set_default_style(store, rewrite_style.strip())
        except ValueError:
            pass  # invalid id — leave existing value unchanged

        # Scheduler settings (unchanged)
        store.set_setting("schedule_cron", schedule_cron.strip())
        store.set_setting(
            "schedule_enabled", "1" if schedule_enabled in ("1", "on", "true")
            else "0")
        return RedirectResponse(url="/settings", status_code=303)

    # ── CLI agent test ──────────────────────────────────────────────────────

    @app.post("/api/test-cli-agent")
    def test_cli_agent(tool_id: str = Form(...)):
        """Test whether a CLI agent tool is installed and operational."""
        path = cli_provider.detect(tool_id)
        if not path:
            return {"found": False, "error": f"未找到 {tool_id}，请确认已安装"}

        td = cli_provider._TOOLS.get(tool_id)
        label = td.label if td else tool_id

        # Try getting version string
        version = None
        for flag in ("--version", "version", "-v"):
            try:
                r = subprocess.run(
                    [path, flag],
                    capture_output=True, encoding="utf-8", errors="replace",
                    timeout=10, check=False,
                )
                out = (r.stdout or r.stderr or "").strip()
                if out:
                    # Take only the first line
                    version = out.splitlines()[0][:120]
                    break
            except (subprocess.TimeoutExpired, OSError):
                continue

        return {
            "found": True,
            "path": path,
            "version": version or "(版本信息不可用)",
            "label": label,
        }

    @app.post("/api/test-llm")
    def test_llm():
        """Test LLM connectivity by sending a minimal chat request."""
        import time as _time

        store = get_store()
        provider = store.get_setting("llm_provider") or config.llm_provider or "mock"
        api_key = store.get_setting("llm_api_key") or config.llm_api_key or ""
        model = store.get_setting("llm_model") or config.llm_model or ""
        api_base = store.get_setting("llm_api_base") or config.llm_api_base or ""

        if provider in ("mock", "", None):
            return {"ok": True, "model": "mock", "latency_ms": 0,
                    "note": "mock 模式无需真实 API"}

        if not api_key:
            return {"ok": False, "error": "未设置 API Key"}

        from app.llm.base import Message
        from app.llm.providers.openai import OpenAIProvider

        t0 = _time.monotonic()
        try:
            p = OpenAIProvider(
                api_key=api_key,
                model=model or "gpt-4o-mini",
                base_url=api_base or None,
                timeout=30,
                max_retries=0,
            )
            reply = p.chat([Message(role="user", content="Reply with exactly: OK")])
            latency = int((_time.monotonic() - t0) * 1000)
            return {"ok": True, "model": model or "gpt-4o-mini",
                    "latency_ms": latency, "reply": (reply or "")[:120]}
        except Exception as exc:
            latency = int((_time.monotonic() - t0) * 1000)
            return {"ok": False, "error": str(exc)[:300], "latency_ms": latency}

    # ── Managed optional runtimes ───────────────────────────────────────────
    @app.get("/api/capabilities")
    def api_capabilities(refresh: bool = False):
        # `refresh` is accepted for compatibility with older frontends. The
        # managed status functions inspect disk state on every call.
        from app.capabilities import get_capabilities
        return get_capabilities(config.data_dir)

    cosyvoice_install_lock = threading.Lock()
    _cosyvoice_events: dict[str, threading.Event] = {}

    @app.get("/api/cosyvoice/status")
    def api_cosyvoice_status():
        from app.cosyvoice_install import setup_status
        return setup_status(config.data_dir)

    @app.post("/api/cosyvoice/setup")
    def api_cosyvoice_setup(validation_only: bool = Form(False)):
        """Install the managed runtime/model payload with SSE progress."""
        import json as _json
        import queue
        import threading as _threading
        from starlette.responses import StreamingResponse
        from app.cosyvoice_install import run_setup, setup_status

        current = setup_status(config.data_dir)
        if current["ready"]:
            return {**current, "ok": True,
                    "message": "CosyVoice runtime 与模型已就绪"}
        if not cosyvoice_install_lock.acquire(blocking=False):
            return {**current, "ok": False, "installing": True,
                    "message": "CosyVoice 正在安装，请等待当前任务完成"}

        evt_q: queue.Queue = queue.Queue()
        setup_config = _current_config(get_store())

        def on_progress(message: str, fraction: float) -> None:
            evt_q.put({"event": "progress", "data": _json.dumps(
                {"message": message, "fraction": round(fraction, 3)},
                ensure_ascii=False)})

        cancel = _threading.Event()
        pause = _threading.Event()
        _cosyvoice_events["cancel"] = cancel
        _cosyvoice_events["pause"] = pause

        def worker() -> None:
            try:
                if setup_config.github_mirror:
                    os.environ["MEDIA_AGENT_GITHUB_MIRROR"] = setup_config.github_mirror
                if setup_config.huggingface_mirror:
                    os.environ["MEDIA_AGENT_HUGGINGFACE_MIRROR"] = setup_config.huggingface_mirror
                result = run_setup(
                    config.data_dir, proxy=setup_config.fetch_proxy,
                    progress_cb=on_progress, cancel_event=cancel,
                    pause_event=pause, validation_only=validation_only)
                evt_q.put({"event": "done", "data": _json.dumps(
                    result, ensure_ascii=False)})
            except Exception as exc:
                current_status = setup_status(config.data_dir)
                evt_q.put({"event": "error", "data": _json.dumps(
                    {**current_status, "ok": False, "message": str(exc)},
                    ensure_ascii=False)})
            finally:
                _cosyvoice_events.clear()
                cosyvoice_install_lock.release()

        _threading.Thread(target=worker, daemon=True).start()

        def sse_frame(event: str, data: str) -> str:
            normalized = data.replace("\r\n", "\n").replace("\r", "\n")
            fields = "".join(
                f"data: {line}\n" for line in normalized.split("\n"))
            return f"event: {event}\n{fields}\n"

        def stream():
            while True:
                try:
                    item = evt_q.get(timeout=.3)
                except queue.Empty:
                    yield sse_frame("heartbeat", "{}")
                    continue
                yield sse_frame(item["event"], item["data"])
                if item["event"] in {"done", "error"}:
                    break

        return StreamingResponse(
            stream(), media_type="text/event-stream; charset=utf-8",
            headers={"X-Accel-Buffering": "no"})

    # ── SadTalker one-click setup ───────────────────────────────────────────
    sadtalker_install_lock = threading.Lock()
    _sadtalker_events: dict[str, threading.Event] = {}

    @app.get("/api/sadtalker/status")
    def api_sadtalker_status():
        """Return current SadTalker setup status (model files, paths, etc.)."""
        from app.video.sadtalker_setup import setup_status
        return setup_status(config.data_dir)

    @app.post("/api/sadtalker/setup")
    def api_sadtalker_setup(
        mirror: bool = Form(True),
        validation_only: bool = Form(False),
    ):
        """Download SadTalker models with resume + auto-configure paths.

        Returns SSE stream of progress events, then a final JSON result.
        """
        from starlette.responses import StreamingResponse
        import queue, json as _json, threading as _threading

        from app.video.sadtalker_setup import run_setup, setup_status

        # Only a complete GPU + runtime + models + import smoke check counts as
        # installed. Model file sizes alone previously produced false positives.
        current = setup_status(config.data_dir)
        if current["ready"]:
            return {
                **current,
                "ok": True,
                "message": "数字人 runtime 与模型已就绪",
            }
        if not sadtalker_install_lock.acquire(blocking=False):
            return {
                **current,
                "ok": False,
                "installing": True,
                "message": "SadTalker 正在安装，请等待当前任务完成",
            }

        evt_q: queue.Queue = queue.Queue()
        # Reload DB-backed settings so the network proxy configured in
        # Settings applies to this large model download.
        setup_config = _current_config(get_store())

        def _on_progress(msg: str, frac: float) -> None:
            evt_q.put({"event": "progress", "data": _json.dumps(
                {"message": msg, "fraction": round(frac, 3)},
                ensure_ascii=False)})

        cancel = _threading.Event()
        pause = _threading.Event()
        _sadtalker_events["cancel"] = cancel
        _sadtalker_events["pause"] = pause

        def _worker() -> None:
            try:
                if setup_config.github_mirror:
                    os.environ["MEDIA_AGENT_GITHUB_MIRROR"] = setup_config.github_mirror
                if setup_config.huggingface_mirror:
                    os.environ["MEDIA_AGENT_HUGGINGFACE_MIRROR"] = setup_config.huggingface_mirror
                result = run_setup(
                    config.data_dir,
                    mirror=mirror,
                    validation_only=validation_only,
                    proxy=setup_config.fetch_proxy,
                    progress_cb=_on_progress,
                    cancel_event=cancel,
                    pause_event=pause,
                )
                evt_q.put({"event": "done",
                            "data": _json.dumps(result, ensure_ascii=False)})
            except Exception as exc:
                current_status = setup_status(config.data_dir)
                evt_q.put({"event": "error",
                            "data": _json.dumps(
                                {**current_status, "ok": False,
                                 "message": str(exc)},
                                ensure_ascii=False)})
            finally:
                _sadtalker_events.clear()
                sadtalker_install_lock.release()

        _threading.Thread(target=_worker, daemon=True).start()

        def _sse_frame(event: str, data: str) -> str:
            # Prefix every physical line as required by the SSE grammar. JSON
            # normally escapes newlines, but this also protects future plain
            # text payloads while preserving Unicode verbatim.
            normalized = data.replace("\r\n", "\n").replace("\r", "\n")
            data_fields = "".join(
                f"data: {line}\n" for line in normalized.split("\n"))
            return f"event: {event}\n{data_fields}\n"

        def _stream():
            while True:
                try:
                    item = evt_q.get(timeout=0.3)
                except queue.Empty:
                    # Send heartbeat every 300ms while waiting
                    yield _sse_frame("heartbeat", "{}")
                    continue
                # Standard SSE framing is two fields:
                #   event: <type>
                #   data: <JSON>
                # The old "<type>: <JSON>" form was not SSE, so the frontend
                # ignored progress/done and falsely reported a broken stream
                # after a successful download.
                yield _sse_frame(item["event"], item["data"])
                if item["event"] in {"done", "error"}:
                    break

        return StreamingResponse(_stream(), media_type="text/event-stream; charset=utf-8",
                                 headers={"X-Accel-Buffering": "no"})

    # ── Pause / Resume / Cancel for running installs ───────────────────────

    @app.post("/api/cosyvoice/pause")
    def api_cosyvoice_pause():
        if not _cosyvoice_events:
            return {"ok": False, "message": "CosyVoice 没有正在运行的安装任务"}
        _cosyvoice_events["pause"].set()
        return {"ok": True, "message": "已暂停"}

    @app.post("/api/cosyvoice/resume")
    def api_cosyvoice_resume():
        if not _cosyvoice_events:
            return {"ok": False, "message": "CosyVoice 没有正在运行的安装任务"}
        _cosyvoice_events["pause"].clear()
        return {"ok": True, "message": "已恢复"}

    @app.post("/api/cosyvoice/cancel")
    def api_cosyvoice_cancel():
        if not _cosyvoice_events:
            return {"ok": False, "message": "CosyVoice 没有正在运行的安装任务"}
        _cosyvoice_events["cancel"].set()
        return {"ok": True, "message": "已取消"}

    @app.post("/api/sadtalker/pause")
    def api_sadtalker_pause():
        if not _sadtalker_events:
            return {"ok": False, "message": "SadTalker 没有正在运行的安装任务"}
        _sadtalker_events["pause"].set()
        return {"ok": True, "message": "已暂停"}

    @app.post("/api/sadtalker/resume")
    def api_sadtalker_resume():
        if not _sadtalker_events:
            return {"ok": False, "message": "SadTalker 没有正在运行的安装任务"}
        _sadtalker_events["pause"].clear()
        return {"ok": True, "message": "已恢复"}

    @app.post("/api/sadtalker/cancel")
    def api_sadtalker_cancel():
        if not _sadtalker_events:
            return {"ok": False, "message": "SadTalker 没有正在运行的安装任务"}
        _sadtalker_events["cancel"].set()
        return {"ok": True, "message": "已取消"}

    # SPA catch-all: serve index.html for client-side deep links that aren't
    # explicit API/asset routes (registered last so real routes win).
    if _spa_enabled:
        @app.get("/{full_path:path}", response_class=HTMLResponse)
        def spa_fallback(full_path: str):
            if full_path.startswith((
                    "api/", "static/", "assets/", "images/", "media/",
                    "videos/", "voices-audio/", "run", "clear/", "export",
                    "import", "sources/", "drafts/", "archive/", "settings",
                    "voices")):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            return FileResponse(str(_spa_index))

    return app
