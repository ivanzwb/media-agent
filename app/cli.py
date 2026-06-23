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
