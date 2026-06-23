import pytest

from app.scheduler import make_scheduler, start_if_enabled


def test_make_scheduler_registers_job():
    sched = make_scheduler(lambda: None, "0 8 * * *")
    try:
        assert len(sched.get_jobs()) == 1
    finally:
        if sched.running:
            sched.shutdown(wait=False)


def test_make_scheduler_invalid_cron_raises():
    with pytest.raises(ValueError):
        make_scheduler(lambda: None, "not a cron")


class FakeStore:
    def __init__(self, settings):
        self._s = settings

    def get_setting(self, key, default=None):
        return self._s.get(key, default)


def test_start_if_enabled_disabled_returns_none():
    store = FakeStore({"schedule_enabled": "0", "schedule_cron": "0 8 * * *"})
    assert start_if_enabled(store, lambda: None) is None


def test_start_if_enabled_starts_when_enabled():
    store = FakeStore({"schedule_enabled": "1", "schedule_cron": "0 8 * * *"})
    sched = start_if_enabled(store, lambda: None)
    assert sched is not None
    try:
        assert sched.running
        assert len(sched.get_jobs()) == 1
    finally:
        sched.shutdown(wait=False)
