"""Tests for ffmpeg encode progress parsing and the build progress tracker.

These cover the machine-readable `-progress` output parsing (_progress_seconds)
and the thread-safe _ProgressTracker used by build_video (per-clip ticks, cap,
finish top-up, throttled emit with ETA, and forced completion).
"""
import time

from app.video.builder import _ProgressTracker, _progress_seconds


# ---- _progress_seconds --------------------------------------------------

def test_progress_seconds_parses_out_time_us():
    assert _progress_seconds("out_time_us=1234567") == 1.234567
    assert _progress_seconds("out_time_us=0") == 0.0
    assert _progress_seconds("out_time_us=12500000") == 12.5


def test_progress_seconds_ignores_other_keys():
    for line in ("frame=123", "fps=30.0", "progress=continue",
                 "speed=1.5x", "bitrate=1234.5kbits/s", ""):
        assert _progress_seconds(line) is None


def test_progress_seconds_invalid_value():
    assert _progress_seconds("out_time_us=abc") is None
    assert _progress_seconds("out_time_us=") is None
    assert _progress_seconds("out_time_us=1.5") is None  # not an int


# ---- _ProgressTracker ----------------------------------------------------

def _tracker(total_work=100.0, cb=None, t0=None):
    return _ProgressTracker(total_work, cb, t0=t0)


def test_tick_accumulates_deltas_with_cap():
    events = []
    t = _tracker(cb=lambda *a: events.append(a))
    t.tick(0, 5.0, cap=10.0)
    t.tick(0, 8.0, cap=10.0)      # +3 (8-5)
    t.tick(0, 20.0, cap=10.0)     # +2 (capped at 10, already at 8)
    assert t._done == 10.0
    assert t._last[0] == 10.0


def test_tick_never_goes_backwards():
    t = _tracker()
    t.tick(1, 5.0, cap=10.0)
    t.tick(1, 3.0, cap=10.0)      # lower than previous → no change
    assert t._done == 5.0


def test_tick_different_indexes_are_independent():
    t = _tracker()
    t.tick(0, 3.0, cap=10.0)
    t.tick(1, 4.0, cap=10.0)
    assert t._done == 7.0


def test_finish_clip_tops_up_to_duration():
    t = _tracker(total_work=10.0)
    t.tick(0, 6.0, cap=10.0)
    t.finish_clip(0, 10.0)
    assert t._last[0] == 10.0
    assert t._done == 10.0


def test_finish_clip_noop_when_already_at_duration():
    t = _tracker(total_work=10.0)
    t.tick(0, 10.0, cap=10.0)
    t.finish_clip(0, 10.0)
    assert t._done == 10.0


def test_add_fixed():
    t = _tracker(total_work=100.0)
    t.add_fixed(25.0)
    t.add_fixed(-5.0)             # negative clamped to 0
    assert t._done == 25.0


def test_emit_reports_pct_and_eta(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    events = []
    t = _ProgressTracker(total_work=100.0, on_progress=lambda p, e: events.append((p, e)),
                         t0=now[0])
    t.add_fixed(50.0)
    t.emit()
    pct, eta = events[-1]
    assert pct == 0.5
    # 50% done in 0s elapsed → eta 0
    assert eta == 0.0


def test_emit_throttled_to_05s(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    events = []
    t = _ProgressTracker(total_work=100.0, on_progress=lambda p, e: events.append(p),
                         t0=0.0)
    t.emit()                      # first: immediate
    t.add_fixed(10.0)
    t.emit()                      # 0.0s later → throttled
    assert len(events) == 1
    now[0] = 1.0
    t.emit()                      # 1.0s later → passes
    assert len(events) == 2


def test_emit_force_bypasses_throttle(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    events = []
    t = _ProgressTracker(total_work=100.0, on_progress=lambda p, e: events.append(p),
                         t0=0.0)
    t.emit()
    t.emit(force=True)
    assert len(events) == 2


def test_emit_eta_none_until_measurable(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    events = []
    t = _ProgressTracker(total_work=1000.0, on_progress=lambda p, e: events.append((p, e)),
                         t0=now[0])
    t.add_fixed(5.0)              # 0.5% → below 1% threshold
    t.emit()
    assert events[-1][1] is None
    t.add_fixed(95.0)             # 10%
    t.emit(force=True)
    assert events[-1][0] == 0.1
    assert events[-1][1] is not None


def test_finish_forces_complete_and_emits():
    events = []
    t = _ProgressTracker(total_work=100.0, on_progress=lambda p, e: events.append((p, e)),
                         t0=0.0)
    t.tick(0, 1.0, cap=10.0)
    t.finish()
    assert t._done == t._total
    assert events[-1] == (1.0, 0.0)


def test_no_callback_when_none():
    t = _ProgressTracker(total_work=10.0, on_progress=None)
    t.tick(0, 1.0, cap=10.0)
    t.finish_clip(0, 10.0)
    t.add_fixed(1.0)
    t.emit()
    t.finish()
    # no exception; _done reflects the work
    assert t._done == t._total


def test_zero_total_does_not_divide_by_zero():
    events = []
    t = _ProgressTracker(total_work=0.0, on_progress=lambda p, e: events.append(p))
    t.finish()
    assert events[-1] == 1.0
