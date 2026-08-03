from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import typer

from app.config import Config
from app.db import connect, init_db
from app.feeds import load_feeds
from app.llm.base import get_provider, get_rewrite_provider
from app.pipeline.orchestrator import run_pipeline
from app.store import Store

app = typer.Typer(help="Media agent pipeline CLI")

logger = logging.getLogger(__name__)

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
        max_drafts: int = typer.Option(0, help="Max articles to rewrite per "
                                       "run (0 = use setting, default 10)"),
        max_age_days: int = typer.Option(0, help="Only keep articles newer "
                                         "than N days (0 = unlimited)"),
        max_per_source: int = typer.Option(0, help="Cap articles per source "
                                           "(0 = unlimited)"),
        with_images: bool = typer.Option(False, "--with-images",
                                         help="Generate cover images")):
    """Run one full pipeline pass."""
    from app.log_setup import quiet_noisy_loggers
    quiet_noisy_loggers()
    cfg = Config.load()
    store = _store()
    feeds_cfg = load_feeds(Path(feeds))
    provider = get_rewrite_provider(cfg.llm_provider, cfg.llm_api_key,
                                    cfg.llm_model, llm_api_base=cfg.llm_api_base,
                                    cli_tool=cfg.cli_tool, timeout=cfg.cli_timeout,
                                    priority=cfg.rewrite_priority)
    image_provider = None
    if with_images:
        from app.images.base import get_image_provider
        image_provider = get_image_provider(cfg.image_provider,
                                            cfg.image_api_key or cfg.llm_api_key,
                                            model=cfg.image_model,
                                            base_url=cfg.image_api_base or cfg.llm_api_base)
    stats = run_pipeline(
        feeds_cfg, store, provider,
        max_drafts=max_drafts or cfg.max_drafts or 10,
        image_provider=image_provider, record=True,
        max_age_days=max_age_days or cfg.max_age_days,
        max_per_source=max_per_source or cfg.max_per_source,
        download_images=cfg.download_images,
        download_videos=cfg.download_videos,
        relevance_filter=cfg.relevance_filter)
    typer.echo(json.dumps(stats, ensure_ascii=False))


@app.command()
def repair_images(
        apply: bool = typer.Option(
            False, "--apply", help="真正写回草稿（默认只预演，不改动文件）")):
    """修复历史草稿里残留的 [[IMG:N]] 记号：能补回图的补回，补不了的清掉。"""
    from app.pipeline.repair import repair_draft_images
    store = _store()
    repairs = repair_draft_images(
        store, store.config, apply=apply, progress=typer.echo)
    if not repairs:
        typer.echo("没有草稿残留 [[IMG:N]] 记号。")
        return
    for repair in repairs:
        typer.echo(f"\n草稿 {repair.draft_id}《{repair.title[:40]}》"
                   f"（{repair.markers} 处记号）")
        for line in repair.resolved:
            typer.echo(f"  补回 {line}")
        for line in repair.skipped:
            typer.echo(f"  清除 {line}（二维码/图标等版面装饰）")
        for token in repair.dropped:
            typer.echo(f"  清除 {token}（找不到对应的图）")
    if not apply:
        typer.echo("\n以上为预演，未改动任何文件。确认无误后加 --apply 执行。")


@app.command()
def serve(host: str = typer.Option("127.0.0.1"),
          port: int = typer.Option(8000),
          feeds: str = typer.Option("feeds.yaml", help="Path to feeds.yaml")):
    """Start the local web UI."""
    import uvicorn
    from app.feeds import ensure_feeds_file
    from app.web.server import create_app
    cfg = Config.load()
    # Anchor the data dir to an absolute path for the whole process so every
    # later Config.load() (in request/worker threads) resolves to the SAME
    # location regardless of the current working directory. Otherwise a fresh
    # Config.load() uses a CWD-relative "data", which can split video/narration
    # files away from the DB and make them appear "lost" after a restart.
    os.environ["MEDIA_AGENT_DATA_DIR"] = str(cfg.data_dir)
    cfg.ensure_dirs()
    ensure_feeds_file(feeds)
    uvicorn.run(create_app(cfg, feeds_path=feeds), host=host, port=port)


def main():
    """Console-script entry point (see pyproject.toml [project.scripts])."""
    app()


if __name__ == "__main__":
    app()
