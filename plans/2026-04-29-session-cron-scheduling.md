# Session Cron Scheduling

**Date**: 2026-04-29
**Status**: Done

## Summary

Add periodic cron scheduling for prompt-mode sessions. Users set a cron schedule (via preset buttons or custom cron expression) from the session detail page. A background asyncio loop checks every 30 seconds for due sessions and auto-launches them using the same logic as the manual Launch button.

## Data Model

Two new columns on the `sessions` table:

- `cron_schedule: VARCHAR(128)` — cron expression string (e.g., `"0 */6 * * *"`), empty string = no schedule
- `cron_next_run: DATETIME` — precomputed next run time (UTC), null = not scheduled

Uses `croniter` library for cron parsing and next-run computation.

## UI

### Session List Table

Two new columns added before Actions:

- **Schedule** — shows the cron label (e.g., "Every 6 hours") linked to the detail page Schedule tab, or "—" for unscheduled/non-prompt sessions
- **Next Run** — shows the next fire time or "Running..." if currently active

### Session Detail Page

New **Schedule** tab (prompt-mode only) with:

- Preset buttons: Every 30 min, Every hour, Every 6h, Every 12h, Daily midnight, Weekdays 9am
- Custom cron expression text input with link to crontab.guru
- Active schedule display with "Cancel Schedule" button
- Next run time display

## Backend

### Endpoints

- `POST /workspaces/{ws_id}/sessions/{sid}/schedule` — validates cron expression, computes next run, saves
- `POST /workspaces/{ws_id}/sessions/{sid}/unschedule` — clears schedule

### Scheduler (`swarmer/scheduler.py`)

Background asyncio loop following the same pattern as `log_poller.py`:

1. Polls every 30 seconds
2. Queries sessions where `cron_next_run <= now`, mode is prompt, and not currently active
3. Calls `_do_launch()` for each due session
4. Advances `cron_next_run` to the next fire time
5. Skips sessions already running (retried next cycle)

### Refactored Launch

Core pod-creation logic extracted from `session_launch` into `_do_launch(session, ws, db)` helper, shared by both the HTTP endpoint and the scheduler.

## Files Changed

- `requirements.txt` — add `croniter==6.2.2`
- `swarmer/models/session.py` — add `cron_schedule`, `cron_next_run`, `cron_label` property
- `swarmer/database.py` — add ALTER TABLE migrations
- `swarmer/routers/sessions.py` — extract `_do_launch`, add schedule/unschedule endpoints
- `swarmer/scheduler.py` — new file, background scheduler loop
- `swarmer/main.py` — start/stop scheduler in lifespan
- `swarmer/templates/sessions/_list_rows.html` — add Schedule and Next Run columns
- `swarmer/templates/sessions/detail.html` — add Schedule tab
- `AGENTS.md` — document scheduler
