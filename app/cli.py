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
        max_drafts: int = typer.Option(10),
        max_age_days: int = typer.Option(0, help="Only keep articles newer "
                                         "than N days (0 = unlimited)"),
        max_per_source: int = typer.Option(0, help="Cap articles per source "
                                           "(0 = unlimited)"),
        with_images: bool = typer.Option(False, "--with-images",
                                         help="Generate cover images")):
    """Run one full pipeline pass."""
    cfg = Config.load()
    store = _store()
    feeds_cfg = load_feeds(Path(feeds))
    provider = get_provider(cfg.llm_provider, cfg.llm_api_key, cfg.llm_model,
                            base_url=cfg.llm_api_base)
    image_provider = None
    if with_images:
        from app.images.base import get_image_provider
        image_provider = get_image_provider(cfg.image_provider,
                                            cfg.image_api_key or cfg.llm_api_key,
                                            model=cfg.image_model,
                                            base_url=cfg.image_api_base or cfg.llm_api_base)
    stats = run_pipeline(
        feeds_cfg, store, provider, max_drafts=max_drafts,
        image_provider=image_provider, record=True,
        max_age_days=max_age_days or cfg.max_age_days,
        max_per_source=max_per_source or cfg.max_per_source)
    typer.echo(json.dumps(stats, ensure_ascii=False))


@app.command()
def serve(host: str = typer.Option("127.0.0.1"),
          port: int = typer.Option(8000),
          feeds: str = typer.Option("feeds.yaml", help="Path to feeds.yaml")):
    """Start the local web UI."""
    import uvicorn
    from app.web.server import create_app
    cfg = Config.load()
    cfg.ensure_dirs()
    uvicorn.run(create_app(cfg, feeds_path=feeds), host=host, port=port)


if __name__ == "__main__":
    app()
