# Plan: Sessions list ACTION column — rename Open to Launch, gate on image availability, auto-refresh every 3s
**Date:** 2026-04-27
**Jira:** [ACM-33311](https://redhat.atlassian.net//browse/ACM-33311)

## Context
The Sessions list page (`/workspaces/{id}/sessions`) currently shows an "Open" link in its Actions column that navigates to the session detail page. The detail page already has a correct "Launch" button that is disabled when the agent image is unavailable and shows a Stop button while a session is active. The list page lags behind in two ways: (1) the action is mislabelled and doesn't actually launch anything, and (2) the 5-second HTMX polling interval is too slow for monitoring running sessions. This work aligns the list page actions with the detail page UX and tightens the refresh cadence to 3 seconds.

## Approach
Three targeted changes:

1. **`swarmer/templates/sessions/list.html`** — Change `hx-trigger="every 5s"` to `hx-trigger="every 3s"` on the `#session-table-wrapper` div.

2. **`swarmer/routers/sessions.py`** — In both `session_list` and `session_list_rows` routes, compute `tool_image_available` (the same dict built in the detail and new-session routes) and pass it into the template context.

3. **`swarmer/templates/sessions/_list_rows.html`** — Replace the simple "Open" anchor in the Actions column with:
   - A **Stop** form (POST → `/stop`) shown only when `s.is_active`
   - A **Launch** form (POST → `/launch`) shown when not active; button is `pf-m-primary` + enabled when the image is available, `pf-m-secondary` + `disabled` when not

## Files to Change
- `swarmer/templates/sessions/list.html` — polling interval 5s → 3s
- `swarmer/routers/sessions.py` — add `tool_image_available` to `session_list` and `session_list_rows` contexts
- `swarmer/templates/sessions/_list_rows.html` — replace "Open" link with Launch/Stop form buttons

## Verification
1. Start dev server (`make run` or `uvicorn`).
2. Navigate to a workspace's Sessions list page.
3. Confirm the Actions column shows "Launch" (not "Open").
4. For a session whose image is unavailable: Launch button should be grey and disabled.
5. For a session whose image is available: Launch button should be blue and clickable; clicking it launches the session.
6. Once a session is running: Actions column should show a yellow "Stop" button.
7. With browser DevTools → Network open, confirm HTMX polling fires every ~3s.
8. Confirm session status badges update automatically as pods transition phases.

---
## Implementation Summary

Implemented as planned with two additions discovered during testing:

- **`session_launch`** gained a `redirect_to` Form field — when `list` is posted from the list page the endpoint redirects back to the sessions list instead of the detail page.
- **`run_duration` model property** fixed: returns live `utcnow() - run_started_at` for active sessions, the final stored duration for completed ones, and `None` for terminal sessions missing `run_completed_at` (e.g. swarmer restarted mid-run). All three duration display sites (`_list_rows.html`, `detail.html`, `workspaces/detail.html`) now prefix the value with ⏱️ when the session is active.
