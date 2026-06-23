from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Config
from app.db import connect, init_db
from app.discovery import discover_from_url, discover_from_keyword
from app.feeds import load_feeds, save_feeds, SourceConfig, Topic
from app.images.base import get_image_provider
from app.llm.base import get_provider
from app.models import Draft
from app.pipeline.adapter import adapt, PLATFORMS
from app.pipeline.orchestrator import run_pipeline
from app.scheduler import start_if_enabled
from app.store import Store

_BASE = Path(__file__).parent
_TEMPLATES = _BASE / "templates"
_STATIC = _BASE / "static"


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
        app.state.scheduler = start_if_enabled(get_store(), run_now)
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

    def run_now():
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
            max_per_source=run_config.max_per_source)

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
    def archive(request: Request, topic: str | None = None):
        store = get_store()
        articles = store.list_articles(limit=200, topic=topic)
        topics = store.list_topics()
        return templates.TemplateResponse(request, "archive.html", {
            "articles": articles, "topics": topics,
            "current_topic": topic, "active": "archive"})

    @app.get("/drafts", response_class=HTMLResponse)
    def drafts_list(request: Request, status: str | None = None):
        store = get_store()
        drafts = store.list_drafts(status=status)
        return templates.TemplateResponse(request, "drafts.html", {
            "drafts": drafts, "current_status": status,
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
            "flagged_claims": body.get("flagged_claims", []) or [],
            "statuses": ["drafted", "reviewing", "approved", "published"],
            "active": "drafts"})

    @app.post("/drafts/{draft_id}")
    def draft_save(draft_id: int, title_candidates: str = Form(""),
                   body_md: str = Form(""), status: str = Form("drafted")):
        store = get_store()
        titles = [t.strip() for t in title_candidates.splitlines() if t.strip()]
        store.update_draft_body(draft_id, titles, body_md, status=status)
        return RedirectResponse(url=f"/drafts/{draft_id}/edit", status_code=303)

    @app.get("/images/{name}")
    def serve_image(name: str):
        path = config.images_dir / name
        if not path.exists():
            return HTMLResponse("not found", status_code=404)
        return FileResponse(str(path))

    @app.get("/sources", response_class=HTMLResponse)
    def sources_page(request: Request):
        cfg = load_feeds(feeds_path) if feeds_path.exists() else None
        return templates.TemplateResponse(request, "sources.html", {
            "feeds": cfg, "active": "sources"})

    @app.post("/sources/add")
    def sources_add(name: str = Form(...), type: str = Form("rss"),
                    url: str = Form(...), topics: str = Form(""),
                    mode: str = Form("single"),
                    include_pattern: str = Form("")):
        cfg = load_feeds(feeds_path)
        topic_list = [t.strip() for t in topics.split(",") if t.strip()]
        cfg.sources.append(SourceConfig(
            name=name, type=type, url=url, topics=topic_list, mode=mode,
            include_pattern=include_pattern or None))
        save_feeds(cfg, feeds_path)
        return RedirectResponse(url="/sources", status_code=303)

    def _append_sources(found):
        cfg = load_feeds(feeds_path)
        existing = {s.url for s in cfg.sources}
        added = 0
        for s in found:
            if s.url not in existing:
                cfg.sources.append(s)
                existing.add(s.url)
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

    # ---- topic management ----

    @app.post("/sources/topics/add")
    def topics_add(name: str = Form(...), keywords: str = Form("")):
        cfg = load_feeds(feeds_path)
        kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
        cfg.topics.append(Topic(name=name.strip(), keywords=kw_list))
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
                     include_pattern: str = Form("")):
        cfg = load_feeds(feeds_path)
        topic_list = [t.strip() for t in topics.split(",") if t.strip()]
        for s in cfg.sources:
            if s.url == url.strip():
                s.name = name.strip()
                s.type = type.strip()
                s.topics = topic_list
                s.mode = mode.strip()
                s.include_pattern = include_pattern.strip() or None
                break
        save_feeds(cfg, feeds_path)
        return RedirectResponse(url="/sources", status_code=303)

    @app.post("/sources/delete")
    def sources_delete(url: str = Form(...)):
        cfg = load_feeds(feeds_path)
        cfg.sources = [s for s in cfg.sources if s.url != url.strip()]
        save_feeds(cfg, feeds_path)
        return RedirectResponse(url="/sources", status_code=303)

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
        run_now()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/drafts/{draft_id}/adapt")
    def draft_adapt(draft_id: int, platform: str = Form(...)):
        store = get_store()
        row = store.get_draft(draft_id)
        meta = store.read_draft_body(draft_id)
        if not row or not meta:
            return HTMLResponse("draft not found", status_code=404)
        titles = meta.get("title_candidates") or ["稿件"]
        run_config = Config.load(store=store)
        provider = get_provider(run_config.llm_provider,
                                run_config.llm_api_key,
                                run_config.llm_model,
                                base_url=run_config.llm_api_base)
        result = adapt(meta.get("body_md", ""), titles[0], platform, provider)
        new_draft = Draft(
            article_id=row["article_id"], platform=platform,
            title_candidates=result["title_candidates"],
            body_md=result["body_md"],
            topic=meta.get("topic", "uncategorized"),
            source_url=meta.get("source_url", ""),
            source_name=meta.get("source_name", ""),
            cover_image=meta.get("cover_image"))
        saved = store.save_draft(new_draft)
        return RedirectResponse(url=f"/drafts/{saved.id}/edit", status_code=303)

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
        db_max_age_days = store.get_setting("max_age_days") or ""
        db_max_per_source = store.get_setting("max_per_source") or ""

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

        return templates.TemplateResponse(request, "settings.html", {
            "llm_provider": db_llm_provider or config.llm_provider,
            "llm_model": db_llm_model or (config.llm_model or ""),
            "llm_api_base": db_llm_api_base or (config.llm_api_base or ""),
            "image_provider": db_image_provider or (config.image_provider or "mock"),
            "image_api_base": db_image_api_base or (config.image_api_base or ""),
            "image_model": db_image_model or (config.image_model or ""),
            "data_dir": str(config.data_dir),
            "max_age_days": db_max_age_days if db_max_age_days and db_max_age_days != "0" else (
                str(config.max_age_days) if config.max_age_days is not None and config.max_age_days > 0 else ""),
            "max_per_source": db_max_per_source if db_max_per_source and db_max_per_source != "0" else (
                str(config.max_per_source) if config.max_per_source is not None and config.max_per_source > 0 else ""),
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
                      max_age_days: str = Form(""),
                      max_per_source: str = Form(""),
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

        # Scheduler settings (unchanged)
        store.set_setting("schedule_cron", schedule_cron.strip())
        store.set_setting(
            "schedule_enabled", "1" if schedule_enabled in ("1", "on", "true")
            else "0")
        return RedirectResponse(url="/settings", status_code=303)

    return app
