from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Config
from app.db import connect, init_db
from app.discovery import discover_from_url, discover_from_keyword
from app.feeds import load_feeds, save_feeds, SourceConfig
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
        feeds_cfg = load_feeds(feeds_path)
        provider = get_provider(config.llm_provider, config.llm_api_key,
                                config.llm_model)
        image_provider = get_image_provider(
            config.image_provider, config.llm_api_key)
        return run_pipeline(
            feeds_cfg, store, provider, image_provider=image_provider,
            record=True, max_age_days=config.max_age_days,
            max_per_source=config.max_per_source)

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
        provider = get_provider(config.llm_provider, config.llm_api_key,
                                config.llm_model)
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
    def settings_page(request: Request):
        store = get_store()
        return templates.TemplateResponse(request, "settings.html", {
            "llm_provider": config.llm_provider,
            "llm_model": config.llm_model,
            "image_provider": config.image_provider or "mock",
            "data_dir": str(config.data_dir),
            "max_age_days": config.max_age_days,
            "max_per_source": config.max_per_source,
            "schedule_cron": store.get_setting("schedule_cron", ""),
            "schedule_enabled": store.get_setting("schedule_enabled", "0") == "1",
            "active": "settings"})

    @app.post("/settings")
    def settings_save(schedule_cron: str = Form(""),
                      schedule_enabled: str = Form("0")):
        store = get_store()
        store.set_setting("schedule_cron", schedule_cron.strip())
        store.set_setting(
            "schedule_enabled", "1" if schedule_enabled in ("1", "on", "true")
            else "0")
        return RedirectResponse(url="/settings", status_code=303)

    return app
