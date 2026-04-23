# Plan: Session Prompt UX Improvements
**Date:** 2026-04-23
**Branch:** ACM-33184

## Context
On the session detail page in prompt mode, the Instruction Prompt textarea is buried inside the Configuration card (left column), while the Last Output card takes the full right column. This makes the prompt feel like a secondary concern instead of the primary input. Additionally, after a prompt finishes, users must manually click "Clean up" to delete the completed pod — a redundant step since there's nothing useful to do with a succeeded pod and the logs are already saved to DB.

## Approach

### 1. Move the Prompt input card above Last Output (right column)
Currently the right column (7-col) shows only the Last Output card for prompt mode. We'll add a new "Prompt" card above it, containing the instruction textarea.

- Remove the `cfg-instruction-section` div from inside `cfg-edit-form` (left column).
- Add a new `<div class="pf-v6-c-card">` in the right column, **above** `last-output-card`, rendered only when `session.mode == 'prompt'`.
- The textarea inside this card gets `form="cfg-edit-form"` and `id="instruction-prompt-input"` so it remains associated with the form for FormData submission (HTML5 form association).
- Update the auto-save JS: the current `form.querySelectorAll('textarea')` loop only finds textareas inside the form DOM. After the IIFE, add a few lines to attach `input`/`blur` listeners to `#instruction-prompt-input`.
- Update the `htmx:afterSwap` handler to also set `instrTextarea.disabled = isActive` for the moved textarea.

### 2. Remove the "Clean up" button
In `htmx:afterSwap`, remove the `{% if session.mode == 'prompt' %}` block that mutates `launch-stop-btn` to "↺ Clean up".

Change launch-stop / launch-form visibility conditions:
```js
// Before (isActive || isSucceeded):
launchStop.style.display  = isActive ? '' : 'none';
launchForm.style.display  = isActive ? 'none' : '';
```
The `isSucceeded` variable and `const isSucceeded = ...` line can be removed entirely since no code will reference it.

### 3. Auto-delete pod when prompt finishes (log_poller)
In `log_poller.py`:
- Add `mode: str` parameter to `start_log_poller` and `_poll_loop`.
- After `_save_to_db` returns a terminal phase **and** `phase == "succeeded"` **and** `mode == "prompt"`, call a new `_auto_cleanup_pod(session_id, pod_name, namespace)` helper before returning.
- `_auto_cleanup_pod`: wraps `k8s.delete_pod(pod_name, namespace)` in try/except, then updates `session.pod_name = None` in DB (phase stays "succeeded").

In `sessions.py` (launch endpoint, line ~525):
- Pass `session.mode` to `start_log_poller`:
  ```python
  log_poller.start_log_poller(session.id, session.pod_name, ws.k8s_namespace, session.mode)
  ```

### 4. Narrow the Configuration card (left column)
Removing the Instruction Prompt section from the config card leaves it with only: mode select, persist/resume checkboxes, model select, and PVC info — all short rows. Shrink the column split from the current **5/7** to **4/8** so the right column gets more room for the Prompt + Last Output cards.

Change:
```html
<!-- left col -->
<div class="pf-v6-l-grid__item pf-m-5-col-on-md" id="left-col">
<!-- right col -->
<div class="pf-v6-l-grid__item pf-m-7-col-on-md" ...>
```
To:
```html
<div class="pf-v6-l-grid__item pf-m-4-col-on-md" id="left-col">
<div class="pf-v6-l-grid__item pf-m-8-col-on-md" ...>
```

### 5. Strip prompt prefix from Last Output display
The pod logs begin with the instruction_prompt text (opencode echoes its input argument). Since the Prompt card now shows that text, remove it from the Last Output view.

In `_last_output.html`, before rendering `session.last_output`:
```jinja
{% set display_output = session.last_output %}
{% if session.instruction_prompt and display_output and display_output.startswith(session.instruction_prompt) %}
  {% set display_output = display_output[session.instruction_prompt | length:] | trim %}
{% endif %}
```
Use `display_output` everywhere `session.last_output` was rendered. This is template-only — no backend change needed.

## Files to Change
- `swarmer/templates/sessions/detail.html` — move prompt textarea card, remove Clean up JS, fix launch-stop conditions
- `swarmer/templates/sessions/_last_output.html` — strip instruction_prompt prefix from displayed output
- `swarmer/log_poller.py` — add `mode` param, `_auto_cleanup_pod` helper
- `swarmer/routers/sessions.py` — pass `session.mode` to `start_log_poller`

## Verification
1. Start a prompt mode session and confirm the new "Prompt" card appears above "Last Output" in the right column.
2. Confirm the textarea auto-saves (blur away after editing — "Saved" indicator should flash).
3. Launch the session; confirm the textarea disables while running and re-enables after completion.
4. Let the prompt complete; confirm:
   - No "Clean up" button appears (launch form shows immediately after badge flips to "✓ Completed").
   - Pod is deleted from Kubernetes automatically (check with `kubectl get pods -n <namespace>`).
   - `session.pod_name` is cleared in DB (pod name no longer shows in status badge area).
5. Verify that "Stop" button still works mid-run (manually stops running pod).

---
## Implementation Summary
**Completed:** 2026-04-23

### What Changed
- `swarmer/templates/sessions/detail.html` — column split narrowed 5/7 → 4/8; `cfg-instruction-section` removed from the config form; new "Prompt" card added above Last Output in the right column (textarea uses `form="cfg-edit-form"` for HTML5 association); auto-save blur/input listeners wired for the moved textarea; `htmx:afterSwap` updated to disable/enable the prompt textarea and drop all `isSucceeded` / "Clean up" logic; Mode and Model dropdowns set to `width:auto` to shrink to content
- `swarmer/templates/sessions/_last_output.html` — simplified to a single `{% set _raw = session.last_output %}` (prompt-stripping template logic removed as unnecessary once the backend seeding was eliminated)
- `swarmer/routers/sessions.py` — removed the `session_launch` block that seeded `last_output` with the prompt text as a placeholder; `session_create` now calls `_get_model_options` and persists the first available model when none is supplied, so `session.model` is never empty after creation; `session_launch` save_config path now guards `session.model` with `if model.strip()` to prevent an empty form value from wiping a previously stored model; `start_log_poller` call passes `session.mode`
- `swarmer/log_poller.py` — `start_log_poller` and `_poll_loop` accept a `mode` parameter; new `_auto_cleanup_pod` helper deletes the Kubernetes pod and clears `session.pod_name` in DB when a prompt-mode session reaches phase `succeeded`

### Tests
- No automated tests exist for the frontend or k8s layer — manual verification per the Verification section above is required

### Known Gaps / Follow-up
- The `syncHeight` JS function still sets `last-output-card` maxHeight to the full left-column height; with the Prompt card now above it the cap is slightly generous, but layout still works correctly
- Prompt-prefix stripping from pod logs was initially implemented as a Jinja2 template filter but removed when the root cause (backend seeding `last_output` with the prompt) was found and fixed instead — cleaner outcome
