from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Query, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles


from app.config import Config
from app.db import connect, init_db
from app.discovery import discover_from_url, discover_from_keyword
from app.feeds import load_feeds, save_feeds, update_feeds, SourceConfig, Topic, FeedsConfig
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
    suggest_subtopics, suggest_keywords, suggest_sources, compute_hotness)
from app.pipeline.score import compute_draft_score
from app.pipeline.localize import localize_one
from app.pipeline.rewriter import rewrite
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
    config = config or Config.load()
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

    def _db_conn() -> sqlite3.Connection:
        c = connect(config.db_path)
        init_db(c)
        return c

    def run_now(progress=None):
        store = get_store()
        # Reload config with DB overrides so settings changes take effect
        run_config = Config.load(store=store)
        feeds_cfg = load_feeds(feeds_path)
        provider = get_rewrite_provider(
            run_config.llm_provider, run_config.llm_api_key,
            run_config.llm_model, llm_api_base=run_config.llm_api_base,
            cli_tool=run_config.cli_tool, timeout=run_config.cli_timeout)
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
            record=True, max_age_days=run_config.max_age_days,
            max_per_source=run_config.max_per_source,
            download_images=run_config.download_images,
            download_videos=run_config.download_videos,
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
    _SOURCE_PROG_RE = re.compile(r"抓取来源 \[(\d+)/(\d+)\]")

    # ---- operations DB-backed registry (survives page refresh) ----
    from app.db import start_op, finish_op, fail_op, _op_log, get_running_ops

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

    @app.post("/archive/{article_id}/refetch")
    def archive_refetch(article_id: int):
        from app.sources.scraper import scrape_single
        from app.pipeline.localize import localize_article
        store = get_store()
        row = store.get_article(article_id)
        if not row:
            return {"ok": False, "error": "文章不存在"}
        try:
            art = scrape_single(row["url"], row["source_name"] or "")
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
                run_config = Config.load(store=ws)
                provider = get_rewrite_provider(
                    run_config.llm_provider, run_config.llm_api_key,
                    run_config.llm_model, llm_api_base=run_config.llm_api_base,
                    cli_tool=run_config.cli_tool,
                    timeout=run_config.cli_timeout)
                image_provider = get_image_provider(
                    run_config.image_provider,
                    run_config.image_api_key or run_config.llm_api_key,
                    model=run_config.image_model,
                    base_url=run_config.image_api_base
                           or run_config.llm_api_base)

                _rewrite_log(article_id, f"正在调用 LLM 进行转写…（风格：{chosen_style.name}）",
                             op_key=op_key, op_conn=op_conn)
                draft = rewrite(art, provider, style=chosen_style,
                                promotion_footer=run_config.promotion_footer)
                _rewrite_log(article_id, "转写完成，进行敏感词过滤…",
                             op_key=op_key, op_conn=op_conn)
                sanitize_draft(draft, load_words(run_config))

                _rewrite_log(article_id, "保存草稿…",
                             op_key=op_key, op_conn=op_conn)
                old = ws.get_draft_for_article(article_id)
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

                if old and old["id"] != saved.id:
                    ws.delete_draft(old["id"])

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
        return {"templates": [t.to_public()
                              for t in editor_components.builtin_templates()]}

    @app.post("/api/editor/templates/{template_id}/apply")
    def api_editor_template_apply(template_id: str):
        """Return the template's markdown outline so the client can insert it
        into the editor (non-destructive — the user saves explicitly)."""
        tpl = editor_components.get_template(template_id)
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
    def api_drafts(status: str | None = None):
        from datetime import datetime, timezone, timedelta
        CST = timezone(timedelta(hours=8))
        store = get_store()
        drafts = store.list_drafts(status=status)
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
                "draft_filename": item.get("draft_filename"),
                "draft_path": item.get("draft_path"),
                "article_published_at": item.get("article_published_at"),
                "updated_at": item.get("updated_at"),
            })
        return {"drafts": out,
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

    @app.post("/api/draft/{draft_id}/agent-edit")
    def draft_agent_edit(draft_id: int, prompt: str = Form(...)):
        """Send a user prompt + current draft content to the configured CLI agent."""
        store = get_store()

        # 1. get draft content — give agent the full file (front-matter + body)
        body = store.read_draft_body(draft_id)
        if not body or "body_md" not in body:
            return JSONResponse({"ok": False, "error": "草稿不存在或内容为空"}, status_code=404)

        row = store.get_draft(draft_id)
        full_md = ""
        if row and row["draft_path"]:
            abs_path = config.data_dir / row["draft_path"]
            if abs_path.exists():
                full_md = abs_path.read_text(encoding="utf-8")
        if not full_md:
            # fallback: reconstruct from body + minimal metadata
            import frontmatter
            full_md = frontmatter.dumps(frontmatter.Post(body["body_md"]))

        # 2. determine active CLI tool
        db_tool = store.get_setting("cli_tool") or ""
        active_tool = db_tool or config.cli_tool or "auto"
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

        # 4. invoke the agent
        # NOTE: we deliberately do NOT pass config.llm_model here — that
        # setting belongs to the text-rewriting LLM provider (OpenAI-
        # compatible), not to the CLI agent.  Passing it would override
        # the CLIProvider's built-in _DEFAULT_MODELS fallback (which
        # already pins a known-working model for opencode).
        provider = cli_provider.CLIProvider(active_tool, timeout=config.cli_timeout)
        messages = [
            Message(role="system", content=(
                "你是一个文章编辑助手。用户会给你一篇完整草稿文件（包含 front-matter 元信息和正文 markdown）"
                "以及修改指令。\n\n"
                "规则：\n"
                "1. 根据指令修改草稿内容，**完整输出修改后的整篇文件（包含 front-matter）**。\n"
                "2. 禁止输出任何修改说明、总结、备注、注释、思考过程、标记符号——你的全部输出将被直接写入文件，任何多余文字都会出现在最终发布内容中，会造成破坏，所以禁止。\n"
                "3. 禁止在正文末尾添加分隔符（如 ---）、注释块、TODO 列表等。\n"
                "4. 用户如果要求插入图片，用标准 Markdown 图片语法 `![描述](图片URL)`。\n"
                "5. 不要用代码块包裹输出。")),
            Message(role="user", content=(
                f"## 当前草稿文件\n```markdown\n{full_md}\n```\n\n"
                f"## 用户指令\n{prompt}")),
        ]

        try:
            result = provider.chat(messages)
        except RuntimeError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

        # Post-process: clean agent output — strip thinking text, code fences,
        # trailing notes, and anything else that would corrupt the draft file.
        # Pass original body as fallback when agent output is pure thinking text.
        _original_body = body.get("body_md", "") if body else ""
        result = _clean_agent_output(result, original_body=_original_body)

        # Parse the cleaned output for DB sync and frontend display.
        # This always succeeds because _clean_agent_output guarantees valid
        # front-matter output.
        import frontmatter as _fm
        _parsed = _fm.loads(result)

        # Backup the current draft file before overwriting
        if row and row["draft_path"]:
            src = config.data_dir / row["draft_path"]
            if src.exists():
                bak_path = src.with_name(src.name + ".agent-bak")
                bak_path.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
                logger.info("agent-edit draft=%d backed up", draft_id)

        # Write agent's full output (including front-matter) directly to the draft file
        if row and row["draft_path"]:
            abs_path = config.data_dir / row["draft_path"]
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text(result, encoding="utf-8")

        # Sync DB metadata so read_draft_body() returns correct data
        _parsed_titles = _parsed.metadata.get("title_candidates", []) or []
        _parsed_body = _parsed.content
        preserved_cn = (
            body.get("title_cn")
            if not _parsed.metadata.get("title_cn")
            else _parsed.metadata.get("title_cn")
        )
        store.update_draft_body(
            draft_id,
            title_candidates=_parsed_titles,
            body_md=_parsed_body,
            status="drafted",
            title_cn=preserved_cn,
        )
        logger.info("agent-edit draft=%d saved OK", draft_id)

        # Return body_md separately for the editor (not the full file)
        return {
            "ok": True,
            "result": result,
            "body_md": _parsed_body,
            "saved": True,
            "backup": True,
        }

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

    # ---- export / import config (sources + settings, no API keys) ----
    _EXPORT_SETTING_KEYS = [
        "llm_provider", "llm_model", "llm_api_base",
        "image_provider", "image_api_base", "image_model",
        "tts_provider", "tts_api_base", "tts_model", "tts_voice",
        "max_age_days", "max_per_source", "download_workers", "video_fit",
        "video_brand_name",
        "sensitive_level", "sensitive_words",
        "promotion_footer",
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
        def _do_add(cfg):
            cfg.add_source(SourceConfig(
                name=name, type=type, url=url, topics=topic_list, mode=mode,
                include_pattern=include_pattern or None,
                exclude_pattern=exclude_pattern or None,
                max_pages=_to_int(max_pages, 1),
                render_js=render_js in ("1", "on", "true")))
        update_feeds(feeds_path, _do_add)
        return RedirectResponse(url="/sources", status_code=303)

    def _append_sources(found):
        added = 0
        def _do_append(cfg):
            nonlocal added
            for s in found:
                if cfg.add_source(s) is not None:
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

    def _resolve_provider(rc: Config | None = None) -> LLMProvider:
        """Get LLM provider respecting cli_tool: Agent (opencode/codex/copilot)
        first, then configured LLM provider, then mock."""
        if rc is None:
            rc = Config.load(store=get_store())
        return get_rewrite_provider(
            rc.llm_provider, rc.llm_api_key,
            rc.llm_model, llm_api_base=rc.llm_api_base,
            cli_tool=rc.cli_tool, timeout=rc.cli_timeout)

    def _build_tts(rc, tts_provider=None, tts_voice=None):
        provider = tts_provider if tts_provider is not None else rc.tts_provider
        voice = tts_voice if tts_voice is not None else rc.tts_voice
        # cosyvoice / fishaudio (voice cloning providers): resolve the
        # selected voice id to its reference sample and pass as speaker_wav.
        if provider == "cosyvoice":
            sp = voice_sample_path(config, voice)
            return get_tts_provider("cosyvoice", voice=str(sp) if sp else None,
                                    model=rc.tts_model)
        if provider == "fishaudio":
            sp = voice_sample_path(config, voice)
            return get_tts_provider(
                "fishaudio",
                api_key=rc.tts_api_key,
                voice=str(sp) if sp else None,
                model=rc.tts_model)
        return get_tts_provider(
            provider, base_url=rc.tts_api_base,
            api_key=rc.tts_api_key, model=rc.tts_model, voice=voice)

    @app.post("/sources/topics/suggest")
    def topics_suggest(themes: str = Form(...)):
        theme_list = [t.strip() for t in re.split(r"[,，\n]", themes) if t.strip()]
        subtopics = suggest_subtopics(theme_list, _resolve_provider())
        return {"themes": theme_list, "subtopics": subtopics}

    @app.post("/sources/topics/keywords")
    def topics_keywords(subtopic: str = Form(...)):
        keywords = suggest_keywords(subtopic.strip(), _resolve_provider())
        return {"subtopic": subtopic.strip(), "keywords": keywords}

    @app.post("/sources/suggest-sources")
    def sources_suggest_frontier(topic: str = Form(...)):
        candidates = suggest_sources(topic.strip(), _resolve_provider())
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
            found.append(SourceConfig(
                name=name, type="rss", url=url, topics=topics))
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
                     render_js: str = Form("")):
        topic_list = [t.strip() for t in topics.split(",") if t.strip()]
        _url = url.strip()
        def _do_edit(cfg):
            for s in cfg.sources:
                if s.url == _url:
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
                run_config = Config.load(store=get_store())
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
                run_config = Config.load(store=get_store())
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

        run_config = Config.load(store=get_store())
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
    def api_list_voices():
        """Return available voice options grouped by provider."""
        from app.tts.voices import list_voices
        cloned = list_voices(config)
        return {
            "kitten": _KITTEN_PROFILES,
            "openai": _OPENAI_VOICES,
            "cosyvoice": [{"id": v["id"], "label": v.get("name", v["id"])}
                          for v in cloned],
            "fishaudio": [{"id": v["id"], "label": v.get("name", v["id"])}
                          for v in cloned],
            "kitten_http": _KITTEN_PROFILES,
        }

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
                rc = Config.load(store=get_store())
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

    # ---- voice library (local voice cloning, cosyvoice) ----
    @app.get("/api/voices")
    def api_voices():
        return {"voices": list_voices(config)}

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
        run_config = Config.load(store=store)
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
        run_config = Config.load(store=store)
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
        run_config = Config.load(store=store)
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
        store = get_store()
        meta = store.read_draft_body(draft_id)
        if not meta:
            return JSONResponse({"error": "draft not found"}, status_code=404)
        run_config = Config.load(store=store)
        from app.wechat import get_wechat_client, WeChatError
        from app.wechat.publish import publish_article, upload_video
        client = get_wechat_client(run_config)
        if client is None:
            return JSONResponse(
                {"ok": False,
                 "error": "未配置公众号 AppID/AppSecret，请到「设置」页填写。"},
                status_code=400)
        try:
            if kind == "video":
                result = upload_video(client, run_config, draft_id, meta)
            else:
                result = publish_article(client, run_config, meta, mode=mode,
                                         theme=theme)
        except WeChatError as exc:
            return JSONResponse(
                {"ok": False,
                 "error": f"微信接口错误 {exc.errcode}: {exc.errmsg}"},
                status_code=502)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)},
                                status_code=500)
        if result.get("ok") and result.get("mode") == "publish":
            try:
                store.set_draft_status(draft_id, "published")
            except Exception:                       # noqa: BLE001
                pass
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

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
        run_config = Config.load(store=store)
        from app.wechat.formatter import render_styled_html, list_themes
        from app.wechat.publish import (
            inline_images_base64, _absolutize_body_md)
        titles = meta.get("title_candidates") or []
        title = (meta.get("title_cn") or (titles[0] if titles else "")).strip()
        body_md = _absolutize_body_md(meta.get("body_md", ""))
        html = render_styled_html(body_md, platform=platform, theme=theme)
        html = inline_images_base64(html, run_config)
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
        run_config = Config.load(store=store)
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
        run_config = Config.load(store=store)
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
            "image_provider": _get("image_provider") or (config.image_provider or "mock"),
            "image_api_base": _get("image_api_base") or (config.image_api_base or ""),
            "image_model": _get("image_model") or (config.image_model or ""),
            "tts_provider": _get("tts_provider") or (config.tts_provider or "kitten"),
            "tts_api_base": _get("tts_api_base") or (config.tts_api_base or ""),
            "tts_model": _get("tts_model") or (config.tts_model or ""),
            "tts_voice": _get("tts_voice") or (config.tts_voice or ""),
            "voices": list_voices(config),
            "data_dir": str(config.data_dir),
            "max_age_days": _get("max_age_days") or (str(config.max_age_days) if config.max_age_days else ""),
            "max_per_source": _get("max_per_source") or (str(config.max_per_source) if config.max_per_source else ""),
            "download_workers": _get("download_workers") or (str(config.download_workers) if config.download_workers else ""),
            "default_workers": os.cpu_count() or 4,
            "video_fit": _get("video_fit") or (config.video_fit or "fit"),
            "video_brand_name": _get("video_brand_name") or (config.video_brand_name or "Media Agent"),
            "sensitive_level": _get("sensitive_level") or (config.sensitive_level or "standard"),
            "sensitive_words": _get("sensitive_words") or (config.sensitive_words or ""),
            "promotion_footer": _get("promotion_footer") or (config.promotion_footer or ""),
            "cli_tool": _get("cli_tool") or (config.cli_tool or "auto"),
            "download_images": (store.get_setting("download_images") or "1") not in ("0", "false", "no", ""),
            "download_videos": (store.get_setting("download_videos") or "1") not in ("0", "false", "no", ""),
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
                      image_provider: str = Form(""),
                      image_api_key: str = Form(""),
                      image_api_base: str = Form(""),
                      image_model: str = Form(""),
                      tts_provider: str = Form(""),
                      tts_api_key: str = Form(""),
                      tts_api_base: str = Form(""),
                      tts_model: str = Form(""),
                      tts_voice: str = Form(""),
                      max_age_days: str = Form(""),
                      max_per_source: str = Form(""),
                      download_workers: str = Form(""),
                        video_fit: str = Form(""),
                        video_brand_name: str = Form(""),
                       sensitive_level: str = Form(""),
                       sensitive_words: str = Form(""),
                       promotion_footer: str = Form(""),
                       wechat_appid: str = Form(""),
                       wechat_appsecret: str = Form(""),
                       wechat_author: str = Form(""),
                       download_images: str = Form("0"),
                       download_videos: str = Form("0"),
                        cli_tool: str = Form(""),
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
            "video_fit": video_fit.strip(),
            "video_brand_name": video_brand_name.strip(),
            "sensitive_level": sensitive_level.strip(),
            "sensitive_words": sensitive_words.strip(),
            "promotion_footer": promotion_footer.strip(),
            "wechat_appid": wechat_appid.strip(),
            "wechat_author": wechat_author.strip(),
            "cli_tool": cli_tool.strip(),
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
