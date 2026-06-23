# Media Agent — Plan 3: Scheduling + Run Controls + Platform Adaptation

> Implement task-by-task with TDD. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add (1) run controls to limit fetched articles by recency and per-source count, (2) platform-adapted rewrites (公众号/小红书/知乎) generated from the master draft, and (3) scheduled automatic runs via APScheduler, configurable from the web UI.

**Architecture:** Extend orchestrator with filtering; add `app/pipeline/adapter.py` for platform rewrites; add `app/scheduler.py` (APScheduler BackgroundScheduler) reading a cron from the `settings` table. Web UI gains adapt buttons and a schedule form.

**Tech Stack (added):** APScheduler 3.10.x.

---

## File Structure

- Modify: `requirements.txt` — add `APScheduler==3.10.4`
- Modify: `app/config.py` — add `max_age_days`, `max_per_source` from env
- Modify: `app/pipeline/orchestrator.py` — `collect_sources(feeds, max_per_source)`, `filter_by_age()`, thread controls through `run_pipeline`
- Create: `app/pipeline/adapter.py` — `adapt(master_body_md, title, platform, provider)`, `PLATFORMS`
- Modify: `app/store.py` — `get_setting`, `set_setting`, `get_draft_meta_for_adapt` (reuse read_draft_body + draft row)
- Create: `app/scheduler.py` — `make_scheduler(func, cron_expr)`, `start_if_enabled(store, runner)`
- Modify: `app/web/server.py` — `POST /drafts/{id}/adapt`, `POST /settings` (schedule), show schedule, start scheduler on startup
- Modify: `app/web/templates/draft_edit.html` — platform adapt buttons
- Modify: `app/web/templates/settings.html` — schedule form + run controls display
- Modify: `app/cli.py` — `run` gets `--max-age-days`, `--max-per-source`; `serve` starts scheduler
- Tests under `tests/`

---

## Task 1: Dependency

- [ ] Add `APScheduler==3.10.4` to `requirements.txt`; install via Tsinghua mirror. Commit.

---

## Task 2: Run controls

**Files:** Modify `app/config.py`, `app/pipeline/orchestrator.py`; Test `tests/test_run_controls.py`

- [ ] **Tests:**
  - `filter_by_age(articles, max_age_days, now)` drops articles older than cutoff, keeps `published_at is None`.
  - `collect_sources(feeds, max_per_source=2)` caps per-source (monkeypatch fetch_feed to return 5 → expect 2).
  - `run_pipeline(..., max_age_days=7, max_per_source=3)` respects both.
- [ ] **Implement:**
  - `Config`: `max_age_days: int | None`, `max_per_source: int | None` from env `MEDIA_AGENT_MAX_AGE_DAYS`, `MEDIA_AGENT_MAX_PER_SOURCE`.
  - `filter_by_age` helper; per-source slice in `collect_sources`; `run_pipeline` accepts and applies them.
- [ ] Run tests, commit.

---

## Task 3: Platform adapter

**Files:** Create `app/pipeline/adapter.py`; Test `tests/test_adapter.py`

- [ ] **Tests:**
  - `PLATFORMS` contains `wechat`, `xiaohongshu`, `zhihu`.
  - `adapt(master_md, title, "xiaohongshu", provider)` returns dict with `title_candidates` and `body_md`; with scripted mock JSON it parses; with non-JSON it falls back to raw text + original title.
  - unknown platform raises ValueError.
- [ ] **Implement** mirroring rewriter's JSON extraction, with per-platform system prompt. Faithfulness constraint repeated (must not invent facts beyond master draft).
- [ ] Run tests, commit.

---

## Task 4: Store settings + adapt integration

**Files:** Modify `app/store.py`; Test extend `tests/test_store_web.py`

- [ ] **Tests:** `set_setting('k','v')` then `get_setting('k')=='v'`; `get_setting('missing', default)` returns default.
- [ ] **Implement** `get_setting`/`set_setting` (upsert into `settings`).
- [ ] Run tests, commit.

---

## Task 5: Web — platform adapt + schedule settings

**Files:** Modify `app/web/server.py`, `templates/draft_edit.html`, `templates/settings.html`; Test extend `tests/test_web.py`

- [ ] **Tests:**
  - `POST /drafts/{id}/adapt` with form `platform=xiaohongshu` (mock LLM) creates a new draft row with `platform='xiaohongshu'`; redirect 303 to new draft edit.
  - `POST /settings` with `schedule_cron` and `schedule_enabled` persists via `set_setting`; `GET /settings` shows the cron.
- [ ] **Implement** routes; adapt reads master draft meta (article_id/topic/source) + body, calls `adapt`, `save_draft`. Settings form writes settings.
- [ ] Run tests, commit.

---

## Task 6: Scheduler

**Files:** Create `app/scheduler.py`; Modify `app/web/server.py`, `app/cli.py`; Test `tests/test_scheduler.py`

- [ ] **Tests:**
  - `make_scheduler(func, "0 8 * * *")` returns a scheduler with one job using a CronTrigger (assert `len(sched.get_jobs())==1`); do not start in test or start then shutdown.
  - invalid cron raises.
- [ ] **Implement:**
  - `make_scheduler(func, cron_expr)`: BackgroundScheduler, `add_job(func, CronTrigger.from_crontab(cron_expr))`, return (not started).
  - `start_if_enabled(store, runner)`: if `get_setting('schedule_enabled')=='1'` and cron set, build+start scheduler with a job that calls `runner()`; return scheduler or None.
  - `create_app`: on startup event, call `start_if_enabled` with a runner that runs the pipeline using configured providers; keep reference on app state.
  - `cli serve`: scheduler starts via app startup (no extra flag needed).
- [ ] Run full suite + manual smoke (serve, set schedule via UI). Commit.

---

## Self-Review Checklist

- Spec coverage: 定时调度(Task6)、运行控制按时间窗/每源条数(Task2)、平台适配改写(Task3/5).
- Type consistency: `run_pipeline` new kw args; `adapt(...)->dict`; `make_scheduler(func, cron)`; store `get_setting/set_setting`.
- Faithfulness preserved: adapter prompts forbid inventing facts beyond the master draft.
