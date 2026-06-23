from __future__ import annotations

from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger


def make_scheduler(func: Callable[[], None], cron_expr: str,
                   job_id: str = "pipeline") -> BackgroundScheduler:
    trigger = CronTrigger.from_crontab(cron_expr)
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(func, trigger=trigger, id=job_id,
                      replace_existing=True)
    return scheduler


def start_if_enabled(store, runner: Callable[[], None]):
    enabled = store.get_setting("schedule_enabled", "0")
    cron_expr = store.get_setting("schedule_cron")
    if enabled != "1" or not cron_expr:
        return None
    try:
        scheduler = make_scheduler(runner, cron_expr)
    except ValueError:
        return None
    scheduler.start()
    return scheduler
