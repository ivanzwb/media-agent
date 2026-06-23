from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Config
from app.db import connect, init_db
from app.feeds import load_feeds, save_feeds, SourceConfig
from app.images.base import get_image_provider
from app.llm.base import get_provider
from app.pipeline.orchestrator import run_pipeline
from app.store import Store

_BASE = Path(__file__).parent
_TEMPLATES = _BASE / "templates"
_STATIC = _BASE / "static"


def create_app(config: Config | None = None,
               feeds_path: str | Path = "feeds.yaml") -> FastAPI:
    config = config or Config.load()
    config.ensure_dirs()
    feeds_path = Path(feeds_path)

    app = FastAPI(title="Media Agent")
    templates = Jinja2Templates(directory=str(_TEMPLATES))
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    def get_store() -> Store:
        conn = connect(config.db_path)
        init_db(conn)
        return Store(conn, config)

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

    @app.post("/run")
    def trigger_run():
        store = get_store()
        feeds_cfg = load_feeds(feeds_path)
        provider = get_provider(config.llm_provider, config.llm_api_key,
                                config.llm_model)
        image_provider = get_image_provider(
            config.image_provider, config.llm_api_key)
        run_pipeline(feeds_cfg, store, provider, image_provider=image_provider,
                     record=True)
        return RedirectResponse(url="/", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        return templates.TemplateResponse(request, "settings.html", {
            "llm_provider": config.llm_provider,
            "llm_model": config.llm_model,
            "image_provider": config.image_provider or "mock",
            "data_dir": str(config.data_dir),
            "active": "settings"})

    return app
