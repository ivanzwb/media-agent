from __future__ import annotations

import json
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Config
from app.db import connect, init_db
from app.discovery import discover_from_url, discover_from_keyword
from app.feeds import load_feeds, save_feeds, SourceConfig, Topic, FeedsConfig
from app.images.base import get_image_provider
from app.llm.base import get_provider
from app.models import Article, Draft
from app.pipeline.adapter import adapt, PLATFORMS
from app.platforms.registry import get as get_platform, list_all as list_platforms
from app.pipeline.images import attach_cover
from app.pipeline.narration import (
    generate_narration, load_narration, save_scenes, resynth_scenes)
from app.pipeline.orchestrator import run_pipeline, _append_prompt
from app.pipeline.recommender import (
    suggest_subtopics, suggest_keywords, suggest_sources, compute_hotness)
from app.pipeline.localize import localize_one
from app.pipeline.rewriter import rewrite
from app.pipeline.sanitizer import load_words, sanitize_draft
from app.pipeline.video import build_explainer_video, video_path
from app.sources.extractor import _images_from_html, _videos_from_html
from app.tts.base import get_tts_provider
from app.tts.voices import (
    list_voices, add_voice, delete_voice, sample_path as voice_sample_path)
from app.scheduler import start_if_enabled
from app.store import Store

_BASE = Path(__file__).parent
_TEMPLATES = _BASE / "templates"
_STATIC = _BASE / "static"


def _to_int(value: str, default: int) -> int:
    try:
        n = int(str(value).strip())
        return n if n > 0 else default
    except (ValueError, TypeError):
        return default


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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.scheduler = start_if_enabled(get_store(), _scheduled_run)
        yield
        sched = getattr(app.state, "scheduler", None)
        if sched is not None and sched.running:
            sched.shutdown(wait=False)

    app = FastAPI(title="Media Agent", lifespan=lifespan)
    templates = Jinja2Templates(directory=str(_TEMPLATES))
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    def get_store() -> Store:
        conn = connect(config.db_path)
        init_db(conn)
        return Store(conn, config)

    def run_now(progress=None):
        store = get_store()
        # Reload config with DB overrides so settings changes take effect
        run_config = Config.load(store=store)
        feeds_cfg = load_feeds(feeds_path)
        provider = get_provider(run_config.llm_provider,
                                run_config.llm_api_key,
                                run_config.llm_model,
                                base_url=run_config.llm_api_base)
        image_provider = get_image_provider(
            run_config.image_provider,
            run_config.image_api_key or run_config.llm_api_key,
            model=run_config.image_model,
            base_url=run_config.image_api_base or run_config.llm_api_base)
        return run_pipeline(
            feeds_cfg, store, provider, image_provider=image_provider,
            record=True, max_age_days=run_config.max_age_days,
            max_per_source=run_config.max_per_source,
            download_media=True, progress=progress)

    # ---- background run state (for live progress / logs) ----
    run_lock = threading.Lock()
    run_state = {
        "running": False, "started_at": None, "finished_at": None,
        "stats": {}, "logs": [], "error": None,
    }

    def _log(msg, stats=None):
        line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
        with run_lock:
            run_state["logs"].append(line)
            # keep only the most recent lines to bound memory
            if len(run_state["logs"]) > 500:
                del run_state["logs"][:-500]
            if stats is not None:
                run_state["stats"] = dict(stats)

    def _scheduled_run():
        """Scheduler callback — mirrors trigger_run so scheduled runs
        show real-time progress and disable the Run Now button."""
        with run_lock:
            if run_state["running"]:
                return  # already running, skip this cycle
            run_state.update(running=True, error=None, stats={}, logs=[],
                             started_at=datetime.now().isoformat(timespec="seconds"),
                             finished_at=None)
        try:
            _log("开始定时运行流水线")
            stats = run_now(progress=_log)
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
    def dashboard(request: Request):
        store = get_store()
        stats = store.dashboard_stats()
        runs = store.list_runs(limit=10)
        drafts = store.list_drafts()[:10]
        return templates.TemplateResponse(request, "dashboard.html", {
            "stats": stats, "runs": runs, "drafts": drafts,
            "active": "dashboard"})

    @app.get("/api/stats")
    def api_stats():
        return get_store().dashboard_stats()

    @app.get("/archive", response_class=HTMLResponse)
    def archive(request: Request, topic: str | None = None,
                source: str | None = None):
        store = get_store()
        articles = store.list_articles(limit=200, topic=topic, source=source)
        topics = store.list_topics()
        sources = store.list_sources()
        # Group articles by date (YYYY-MM-DD) for collapsible date nodes.
        from collections import OrderedDict
        groups: list[tuple[str, list]] = []
        by_date: dict[str, list] = OrderedDict()
        for a in articles:
            dt = a["published_at"] or a["fetched_at"]
            date_key = dt[:10] if dt else "未知日期"
            by_date.setdefault(date_key, []).append(a)
        for date_key in sorted(by_date, reverse=True):
            # Sort day's articles by source_name so same source clusters together
            day_articles = sorted(by_date[date_key], key=lambda a: a["source_name"] or "")
            groups.append((date_key, day_articles))
        return templates.TemplateResponse(request, "archive.html", {
            "articles": articles, "topics": topics, "sources": sources,
            "draft_map": store.drafts_by_article(),
            "current_topic": topic, "current_source": source,
            "active": "archive",
            "date_groups": groups})

    @app.get("/archive/{article_id}/view", response_class=HTMLResponse)
    def archive_view(request: Request, article_id: int):
        store = get_store()
        row = store.get_article(article_id)
        if not row:
            return HTMLResponse("文章不存在", status_code=404)
        body = store.read_article_body(article_id)
        return templates.TemplateResponse(request, "article_view.html", {
            "article": row, "meta": body,
            "content_md": body.get("content_md", ""),
            "images": body.get("images", []) or [],
            "videos": body.get("videos", []) or [],
            "active": "archive"})

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
            localize_article(art, store.config)
            localized = sum(1 for u in art.images + art.videos if u.startswith("/media/"))
        except Exception:                          # noqa: BLE001
            pass
        store.update_article_archive(article_id, art.content_md, art.images,
                                     art.videos)
        return {"ok": True, "images": len(art.images),
                "videos": len(art.videos), "localized": localized}

    @app.post("/archive/{article_id}/rewrite")
    def archive_rewrite(article_id: int):
        store = get_store()
        row = store.get_article(article_id)
        if not row:
            return {"ok": False, "error": "文章不存在"}
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

        run_config = Config.load(store=store)
        provider = get_provider(run_config.llm_provider,
                                run_config.llm_api_key, run_config.llm_model,
                                base_url=run_config.llm_api_base)
        image_provider = get_image_provider(
            run_config.image_provider,
            run_config.image_api_key or run_config.llm_api_key,
            model=run_config.image_model,
            base_url=run_config.image_api_base or run_config.llm_api_base)

        # Create-then-delete: save new draft first, then remove old one.
        # This prevents draft loss if rewrite() or save_draft() fails.
        draft = rewrite(art, provider)
        sanitize_draft(draft, load_words(run_config))
        saved = store.save_draft(draft)

        old = store.get_draft_for_article(article_id)
        if old and old["id"] != saved.id:
            store.delete_draft(old["id"])
        if image_provider is not None:
            attach_cover(saved, image_provider, store.config.images_dir)
            if saved.cover_image:
                store.set_draft_cover(saved.id, saved.cover_image)
            else:
                _append_prompt(saved, store)
        else:
            _append_prompt(saved, store)
        return {"ok": True, "draft_id": saved.id}

    @app.get("/drafts", response_class=HTMLResponse)
    def drafts_list(request: Request, status: str | None = None):
        from datetime import datetime, timezone, timedelta
        CST = timezone(timedelta(hours=8))
        store = get_store()
        drafts = store.list_drafts(status=status)
        enriched = []
        for d in drafts:
            item = dict(d)
            if not item.get("title_cn"):
                body = store.read_draft_body(item["id"])
                candidates = body.get("title_candidates", []) or []
                item["display_title"] = candidates[0] if candidates else None
            else:
                item["display_title"] = None
            # Convert updated_at UTC → local (CST)
            raw = item.get("updated_at", "")
            if raw:
                try:
                    dt = datetime.fromisoformat(str(raw))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    item["updated_at"] = dt.astimezone(CST).strftime("%Y-%m-%d %H:%M")
                except Exception:                          # noqa: BLE001
                    pass
            enriched.append(item)
        return templates.TemplateResponse(request, "drafts.html", {
            "drafts": enriched, "current_status": status,
            "statuses": ["drafted", "reviewing", "approved", "published"],
            "active": "drafts"})

    @app.get("/drafts/{draft_id}/edit", response_class=HTMLResponse)
    def draft_edit(request: Request, draft_id: int):
        store = get_store()
        row = store.get_draft(draft_id)
        body = store.read_draft_body(draft_id)
        title_candidates = body.get("title_candidates", []) or []
        return templates.TemplateResponse(request, "draft_edit.html", {
            "draft": row, "meta": body,
            "title_candidates_text": "\n".join(title_candidates),
            "body_md": body.get("body_md", ""),
            "title_cn": body.get("title_cn") or (title_candidates[0] if title_candidates else ""),
            "flagged_claims": body.get("flagged_claims", []) or [],
            "sensitive_hits": body.get("sensitive_hits", []) or [],
            "narration": load_narration(draft_id, config),
            "has_video": video_path(draft_id, config) is not None,
            "statuses": ["drafted", "reviewing", "approved", "published"],
            "video_brand_name": config.video_brand_name or "Media Agent",
            "article_title": (title_candidates[0] if title_candidates else ""),
            "tts_provider": config.tts_provider,
            "tts_voice": config.tts_voice,
            "platforms": list_platforms(),
            "active": "drafts"})

    @app.post("/drafts/{draft_id}")
    def draft_save(draft_id: int, title_candidates: str = Form(""),
                   body_md: str = Form(""), status: str = Form("drafted"),
                   title_cn: str = Form("")):
        store = get_store()
        titles = [t.strip() for t in title_candidates.splitlines() if t.strip()]
        store.update_draft_body(draft_id, titles, body_md, status=status,
                                title_cn=title_cn.strip() or None)
        return RedirectResponse(url=f"/drafts/{draft_id}/edit", status_code=303)

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
    def sources_page(request: Request):
        cfg = load_feeds(feeds_path) if feeds_path.exists() else None
        store = get_store()
        raw_hot = store.get_setting("hotness_data")
        hotness = json.loads(raw_hot) if raw_hot else None
        hotness_updated = store.get_setting("hotness_updated_at")
        return templates.TemplateResponse(request, "sources.html", {
            "feeds": cfg, "active": "sources",
            "hotness": hotness,
            "hotness_updated": hotness_updated,
        })

    @app.post("/sources/add")
    def sources_add(name: str = Form(...), type: str = Form("rss"),
                    url: str = Form(...), topics: str = Form(""),
                    mode: str = Form("single"),
                    include_pattern: str = Form(""),
                    exclude_pattern: str = Form(""),
                    max_pages: str = Form("1"),
                    render_js: str = Form("")):
        cfg = load_feeds(feeds_path)
        topic_list = [t.strip() for t in topics.split(",") if t.strip()]
        cfg.add_source(SourceConfig(
            name=name, type=type, url=url, topics=topic_list, mode=mode,
            include_pattern=include_pattern or None,
            exclude_pattern=exclude_pattern or None,
            max_pages=_to_int(max_pages, 1),
            render_js=render_js in ("1", "on", "true")))
        save_feeds(cfg, feeds_path)
        return RedirectResponse(url="/sources", status_code=303)

    def _append_sources(found):
        cfg = load_feeds(feeds_path)
        added = 0
        for s in found:
            if cfg.add_source(s) is not None:
                added += 1
        if added:
            save_feeds(cfg, feeds_path)
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
                                   provider=_llm_provider())
        except Exception:
            # Fallback: store-only with no provider
            data = compute_hotness(store)
        store.set_setting("hotness_data", json.dumps(data))
        store.set_setting("hotness_updated_at",
                          datetime.now(timezone.utc).isoformat())
        return {"status": "ok", **data}

    # ---- smart topic recommendation ----

    def _llm_provider():
        store = get_store()
        run_config = Config.load(store=store)
        return get_provider(run_config.llm_provider,
                            run_config.llm_api_key,
                            run_config.llm_model,
                            base_url=run_config.llm_api_base)

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
        subtopics = suggest_subtopics(theme_list, _llm_provider())
        return {"themes": theme_list, "subtopics": subtopics}

    @app.post("/sources/topics/keywords")
    def topics_keywords(subtopic: str = Form(...)):
        keywords = suggest_keywords(subtopic.strip(), _llm_provider())
        return {"subtopic": subtopic.strip(), "keywords": keywords}

    @app.post("/sources/suggest-sources")
    def sources_suggest_frontier(topic: str = Form(...)):
        candidates = suggest_sources(topic.strip(), _llm_provider())
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

    # ---- topic management ----

    @app.post("/sources/topics/add")
    def topics_add(name: str = Form(...), keywords: str = Form("")):
        cfg = load_feeds(feeds_path)
        kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
        cfg.add_topic(Topic(name=name.strip(), keywords=kw_list))
        save_feeds(cfg, feeds_path)
        return RedirectResponse(url="/sources", status_code=303)

    @app.post("/sources/topics/edit")
    def topics_edit(old_name: str = Form(...), name: str = Form(...),
                    keywords: str = Form("")):
        cfg = load_feeds(feeds_path)
        kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
        for t in cfg.topics:
            if t.name == old_name.strip():
                t.name = name.strip()
                t.keywords = kw_list
                break
        save_feeds(cfg, feeds_path)
        return RedirectResponse(url="/sources", status_code=303)

    @app.post("/sources/topics/delete")
    def topics_delete(name: str = Form(...)):
        cfg = load_feeds(feeds_path)
        cfg.topics = [t for t in cfg.topics if t.name != name.strip()]
        save_feeds(cfg, feeds_path)
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
        cfg = load_feeds(feeds_path)
        topic_list = [t.strip() for t in topics.split(",") if t.strip()]
        for s in cfg.sources:
            if s.url == url.strip():
                s.name = name.strip()
                s.type = type.strip()
                s.topics = topic_list
                s.mode = mode.strip()
                s.include_pattern = include_pattern.strip() or None
                s.exclude_pattern = exclude_pattern.strip() or None
                s.max_pages = _to_int(max_pages, 1)
                s.render_js = render_js in ("1", "on", "true")
                break
        save_feeds(cfg, feeds_path)
        return RedirectResponse(url="/sources", status_code=303)

    @app.post("/sources/delete")
    def sources_delete(url: str = Form(...)):
        cfg = load_feeds(feeds_path)
        cfg.sources = [s for s in cfg.sources if s.url != url.strip()]
        save_feeds(cfg, feeds_path)
        return RedirectResponse(url="/sources", status_code=303)

    @app.post("/sources/delete-batch")
    async def sources_delete_batch(request: Request):
        body = await request.json()
        urls = body.get("items", [])
        if not urls:
            return {"deleted": 0}
        cfg = load_feeds(feeds_path)
        url_set = set(u.strip() for u in urls)
        before = len(cfg.sources)
        cfg.sources = [s for s in cfg.sources if s.url not in url_set]
        save_feeds(cfg, feeds_path)
        return {"deleted": before - len(cfg.sources)}

    @app.post("/sources/topics/delete-batch")
    async def topics_delete_batch(request: Request):
        body = await request.json()
        names = body.get("items", [])
        if not names:
            return {"deleted": 0}
        cfg = load_feeds(feeds_path)
        name_set = set(n.strip() for n in names)
        before = len(cfg.topics)
        cfg.topics = [t for t in cfg.topics if t.name not in name_set]
        save_feeds(cfg, feeds_path)
        return {"deleted": before - len(cfg.topics)}

    @app.post("/sources/toggle")
    def sources_toggle(url: str = Form(...)):
        cfg = load_feeds(feeds_path)
        for s in cfg.sources:
            if s.url == url.strip():
                s.enabled = not s.enabled
                break
        save_feeds(cfg, feeds_path)
        return RedirectResponse(url="/sources", status_code=303)

    @app.post("/run")
    def trigger_run():
        with run_lock:
            if run_state["running"]:
                return {"started": False, "running": True,
                        "message": "已有运行正在进行"}
            run_state.update(running=True, error=None, stats={}, logs=[],
                             started_at=datetime.now().isoformat(timespec="seconds"),
                             finished_at=None)

        def worker():
            try:
                _log("开始运行流水线")
                stats = run_now(progress=_log)
                with run_lock:
                    if stats:
                        run_state["stats"] = dict(stats)
                _log("运行成功结束", run_state["stats"])
            except Exception as e:  # noqa: BLE001 - surface to UI log
                with run_lock:
                    run_state["error"] = str(e)
                _log(f"运行出错：{e}")
            finally:
                with run_lock:
                    run_state["running"] = False
                    run_state["finished_at"] = datetime.now().isoformat(
                        timespec="seconds")

        threading.Thread(target=worker, daemon=True).start()
        return {"started": True, "running": True}

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
            }

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
                llm = get_provider(run_config.llm_provider,
                                   run_config.llm_api_key, run_config.llm_model,
                                   base_url=run_config.llm_api_base)
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
            try:
                stats = localize_one(article_id, get_store(), config,
                                     progress=_mlz_log)
                with mlz_lock:
                    mlz_state["stats"] = stats
                    mlz_state["done"] = True
            except Exception as e:  # noqa: BLE001
                with mlz_lock:
                    mlz_state["error"] = str(e)
                _mlz_log(f"出错：{e}")
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
        store = get_store()
        meta = store.read_draft_body(draft_id)
        if not meta:
            return JSONResponse({"error": "draft not found"}, status_code=404)
        titles = meta.get("title_candidates") or ["稿件"]
        run_config = Config.load(store=store)
        provider = get_provider(run_config.llm_provider,
                                run_config.llm_api_key,
                                run_config.llm_model,
                                base_url=run_config.llm_api_base)
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

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, response: Response):
        store = get_store()
        # Prevent browser caching so saved settings always appear
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        # Read DB overrides to pre-fill the form
        db_llm_provider = store.get_setting("llm_provider") or ""
        db_llm_model = store.get_setting("llm_model") or ""
        db_llm_api_base = store.get_setting("llm_api_base") or ""
        db_image_provider = store.get_setting("image_provider") or ""
        db_image_api_base = store.get_setting("image_api_base") or ""
        db_image_model = store.get_setting("image_model") or ""
        db_tts_provider = store.get_setting("tts_provider") or ""
        db_tts_api_base = store.get_setting("tts_api_base") or ""
        db_tts_model = store.get_setting("tts_model") or ""
        db_tts_voice = store.get_setting("tts_voice") or ""
        db_max_age_days = store.get_setting("max_age_days") or ""
        db_max_per_source = store.get_setting("max_per_source") or ""
        db_download_workers = store.get_setting("download_workers") or ""
        db_video_fit = store.get_setting("video_fit") or ""
        db_video_brand_name = store.get_setting("video_brand_name") or ""
        db_sensitive_level = store.get_setting("sensitive_level") or ""
        db_sensitive_words = store.get_setting("sensitive_words") or ""
        db_promotion_footer = store.get_setting("promotion_footer") or ""

        # API key: indicate whether set, mask the value
        api_key_val = store.get_setting("llm_api_key")
        api_key_set = bool(api_key_val and api_key_val.strip())
        if api_key_set:
            masked = api_key_val[:4] + "..." + api_key_val[-4:] if len(api_key_val) > 8 else "****"
        else:
            masked = ""

        # Image API key mask
        img_key_val = store.get_setting("image_api_key")
        img_key_set = bool(img_key_val and img_key_val.strip())
        if img_key_set:
            img_masked = img_key_val[:4] + "..." + img_key_val[-4:] if len(img_key_val) > 8 else "****"
        else:
            img_masked = ""

        # TTS API key mask
        tts_key_val = store.get_setting("tts_api_key")
        tts_key_set = bool(tts_key_val and tts_key_val.strip())
        if tts_key_set:
            tts_masked = tts_key_val[:4] + "..." + tts_key_val[-4:] if len(tts_key_val) > 8 else "****"
        else:
            tts_masked = ""

        return templates.TemplateResponse(request, "settings.html", {
            "llm_provider": db_llm_provider or config.llm_provider,
            "llm_model": db_llm_model or (config.llm_model or ""),
            "llm_api_base": db_llm_api_base or (config.llm_api_base or ""),
            "image_provider": db_image_provider or (config.image_provider or "mock"),
            "image_api_base": db_image_api_base or (config.image_api_base or ""),
            "image_model": db_image_model or (config.image_model or ""),
            "tts_provider": db_tts_provider or (config.tts_provider or "kitten"),
            "tts_api_base": db_tts_api_base or (config.tts_api_base or ""),
            "tts_model": db_tts_model or (config.tts_model or ""),
            "tts_voice": db_tts_voice or (config.tts_voice or ""),
            "tts_voice_display": _voice_display_name(db_tts_voice, config),
            "tts_key_set": tts_key_set,
            "tts_key_masked": tts_masked,
            "voices": list_voices(config),
            "data_dir": str(config.data_dir),
            "max_age_days": db_max_age_days if db_max_age_days and db_max_age_days != "0" else (
                str(config.max_age_days) if config.max_age_days is not None and config.max_age_days > 0 else ""),
            "max_per_source": db_max_per_source if db_max_per_source and db_max_per_source != "0" else (
                str(config.max_per_source) if config.max_per_source is not None and config.max_per_source > 0 else ""),
            "download_workers": db_download_workers if db_download_workers and db_download_workers != "0" else (
                str(config.download_workers) if config.download_workers else ""),
            "default_workers": os.cpu_count() or 4,
            "video_fit": db_video_fit or (config.video_fit or "fit"),
            "video_brand_name": db_video_brand_name or (config.video_brand_name or "Media Agent"),
            "sensitive_level": db_sensitive_level or (config.sensitive_level or "standard"),
            "sensitive_words": db_sensitive_words or (config.sensitive_words or ""),
            "promotion_footer": db_promotion_footer or (config.promotion_footer or ""),
            "api_key_set": api_key_set,
            "api_key_masked": masked,
            "img_key_set": img_key_set,
            "img_key_masked": img_masked,
            "schedule_cron": store.get_setting("schedule_cron", ""),
            "schedule_enabled": store.get_setting("schedule_enabled", "0") == "1",
            "active": "settings"})

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

        # Scheduler settings (unchanged)
        store.set_setting("schedule_cron", schedule_cron.strip())
        store.set_setting(
            "schedule_enabled", "1" if schedule_enabled in ("1", "on", "true")
            else "0")
        return RedirectResponse(url="/settings", status_code=303)

    return app
