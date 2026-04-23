# Plan: Session Launch UI Refactor
**Date:** 2026-04-22
**Branch:** main

## Context
The session detail page has several UX issues to clean up:
1. "Privileged (root)" checkbox is exposed in the UI but should be removed (security best practice).
2. The launch area has one button per agent tool — replacing it with a single launch button + image dropdown is cleaner and scales better.
3. The opencode-python and opencode-golang variants are being collapsed into a single `opencode` image that supports both, so the tool list shrinks to: **opencode** and **crush**.
4. Image/tool choice should be persisted across relaunches (same pattern as model selection, which is already stored in `session.agent_tool`).
5. The launch button should show ✓ or ✗ in the image dropdown entries to indicate registry availability at a glance.
6. GitHub PAT selector clutters the Configuration card — move it to the Git Repositories card header where it is contextually relevant.
7. The repo list table is hard to read in a narrow column — replace with a compact vertical list and a hidden add form revealed by a + button.

## Approach

### 1. Remove Privileged (root) from UI
- Remove the checkbox block from `swarmer/templates/sessions/new.html` (lines 163–173).
- Remove the checkbox block from `swarmer/templates/sessions/detail.html` (lines 186–196).
- Leave the `privileged` DB column and launch logic untouched (always `False` now); no migration needed.

### 2. Consolidate agent tool files
- Rename `opencode-golang` → `opencode` in `opencode.py` (`name` property + `display_name`).
- Remove `opencode_python.py` (it's just a subclass that swaps the image config key).
- Add `agent_image_opencode` as the single image setting (already exists); remove `agent_image_python` usage.
- Update `registry.py` `_init()` to drop `PythonStrategy` import/registration.
- Update `_ALIASES` dict: keep `"opencode": "opencode"` or just remove the alias.
- Update `AGENT_TOOLS` tuple in `session.py` → `("opencode", "crush")`.
- Update `agent_tool` default in `session.py` → `"opencode"`.

### 3. New launch UI in detail.html
Replace the `{% for tool in agent_tools %}` multi-button block with:
```
[▶ Launch]  [dropdown: ✓ OpenCode | ✗ Crush]
```
- A `<select id="launch-tool-select">` with one `<option>` per tool.
  - Each option label: `✓ <display_name>` or `✗ <display_name>` based on `tool_image_available[tool.name]`.
  - Options with unavailable images are disabled.
  - The option matching `session.agent_tool` is pre-selected (remembers last choice).
- A single submit button that posts to the launch endpoint with `agent_tool` taken from the dropdown.
- The existing JS that merges form data already runs on `launch-tool-btn` click; adjust to read the select value before submitting.

### 4. new.html — image dropdown on create
- On the "New Session" form, also replace any agent_tool selection (currently hidden or absent) with the same dropdown pattern showing availability.
- Default to `opencode`.

### 5. Image availability pill
- Remove ✓/✗ from inside the dropdown option text (options cannot be styled reliably).
- Add a green/red PatternFly label pill (`pf-m-green` / `pf-m-red`) after the dropdown showing current selection's availability.
- Pill updates on dropdown `change` and on each HTMX status poll via `afterSwap`.

### 6. GitHub PAT → Git Repositories header
- Replace the visible PAT `<select>` in the Configuration form with a hidden `<input type="hidden" id="cfg_pat">`.
- Extract a `_pat_select.html` partial containing a labelled `<select>` that syncs the hidden input and calls `window._cfgSave()` on change.
- Include the partial in both Git Repositories card headers (prompt-mode left column and non-prompt right column).
- Expose `window._cfgSave = doSave` inside the auto-save IIFE so the out-of-form select can trigger saves.

### 7. Repo list redesign
- Drop the table entirely; replace with a flex-based vertical list of items.
- Each item: repo URL + Public/Private pill on line 1; `branch → /workspace/path` in muted text on line 2; write-access warning inline on line 2; delete ✕ button floated right.
- Remove "No repositories configured." empty state text.
- Replace the always-visible add form with a `+` button; clicking it hides the button and reveals a stacked 3-row form (URL / Branch / Local path) with right-aligned labels. Cancel re-hides the form and restores the button. After HTMX swap the form resets automatically.

## Files to Change
- `swarmer/agent_tools/opencode.py` — rename tool to `opencode`, update `display_name`
- `swarmer/agent_tools/opencode_python.py` — delete (or gut to a stub for migration safety; delete preferred)
- `swarmer/agent_tools/registry.py` — remove PythonStrategy registration, update alias
- `swarmer/models/session.py` — update `AGENT_TOOLS` tuple and `agent_tool` default
- `swarmer/templates/sessions/detail.html` — remove privileged row; replace multi-launch-buttons with single button + dropdown
- `swarmer/templates/sessions/new.html` — remove privileged checkbox; add image dropdown (with availability, defaulting to opencode)
- `swarmer/config.py` — optionally remove `agent_image_python` (check if used anywhere else first)
- `swarmer/routers/sessions.py` — update any hardcoded `"opencode-golang"` / `"opencode-python"` references
- `swarmer/templates/sessions/_pat_select.html` — new partial for PAT dropdown in Git Repos header
- `swarmer/templates/sessions/_repo_list.html` — redesign repo list and add form

## Verification
1. Start the dev server and open a session detail page — confirm only one launch button + dropdown is shown.
2. Select crush from dropdown, launch — confirm correct image is used (or blocked with ✗ if unavailable).
3. Re-open the session detail page — confirm the dropdown pre-selects the last-used tool.
4. Confirm "Privileged (root)" row is gone from both new session and detail pages.
5. Run `grep -r "opencode-python\|opencode-golang\|privileged" swarmer/templates/` to confirm no stragglers.
6. Check that existing sessions with `agent_tool="opencode-golang"` in DB are handled by the alias in registry (or migrated).

---
## Implementation Summary
**Completed:** 2026-04-22

### What Changed
- `swarmer/agent_tools/opencode.py` — renamed `name` from `"opencode-golang"` → `"opencode"`, `display_name` from `"OpenCode (Golang)"` → `"OpenCode"`
- `swarmer/agent_tools/opencode_python.py` — deleted; was a trivial subclass that only swapped the image config key
- `swarmer/agent_tools/registry.py` — removed `PythonStrategy` registration; added `_ALIASES` entries `"opencode-golang"` and `"opencode-python"` → `"opencode"` for backwards compat with existing DB rows
- `swarmer/models/session.py` — `AGENT_TOOLS` is now `("opencode", "crush")`; `agent_tool` column default is `"opencode"`
- `swarmer/config.py` — `default_agent_tool` default changed to `"opencode"`; `agent_image_python` field retained but unused
- `swarmer/routers/sessions.py` — removed `privileged` Form param from `session_create`, `session_edit`, `session_launch`; all tool name defaults updated to `"opencode"`; `get_tool().name` used everywhere to normalize through aliases; `session_new` now checks image availability and passes `tool_image_available` to the template; `session_detail` resolves `canonical_agent_tool` (maps old `"opencode-golang"` DB values to `"opencode"` for correct dropdown pre-selection)
- `swarmer/templates/sessions/detail.html` — removed "Privileged (root)" checkbox row; replaced per-tool launch button loop with single **▶ Launch** button + `<select>` dropdown; green/red pill after dropdown shows live availability; `data-availability` JSON attribute drives JS state; `canonical_agent_tool` drives pre-selection; JS launch handler reads from select; `afterSwap` HTMX handler syncs `launch-btn`, `launch-tool-select`, and availability pill; `cfg_agent_tool` hidden field uses `canonical_agent_tool`; GitHub PAT replaced with hidden input; both Git Repos card headers include `_pat_select.html` partial; `window._cfgSave` exposed from auto-save IIFE
- `swarmer/templates/sessions/new.html` — removed "Privileged (run as root)" checkbox and `<hr>` separator; agent tool `<select>` now shows `✓`/`✗` per image and disables unavailable options
- `swarmer/templates/sessions/_pat_select.html` — new partial: labelled PAT `<select>` that syncs hidden `cfg_pat` input and triggers auto-save via `window._cfgSave`
- `swarmer/templates/sessions/_repo_list.html` — replaced table with compact flex list (URL + badges / branch → path per item); empty state text removed; add form hidden behind `+` button with Cancel to dismiss

### Tests
- No automated tests exist for the UI layer; manual verification steps in the Verification section above.

### Known Gaps / Follow-up
- `agent_image_python` config field is retained but no longer referenced — can be removed in a follow-up cleanup once confirmed no deployments set it.
- Existing DB rows with `agent_tool="opencode-golang"` are handled transparently via the registry alias but are not proactively migrated; they will self-correct to `"opencode"` on next launch (the launch handler normalizes via `get_tool().name`).
- The `privileged` DB column and K8s pod spec parameter remain wired up (always `False`) — a future cleanup ticket (ACM-33117 area) could remove them entirely once the schema migration is safe.
