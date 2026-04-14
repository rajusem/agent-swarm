# Model ID Fixes & All-Mode Model Selection — 2026-04-14

## Plan

### Problem
The google-vertex-anthropic provider in opencode uses a different model ID format
from what was originally coded. Verified by inspecting `/root/.local/state/opencode/model.json`
inside a running pod after manually selecting a model in the opencode TUI.

Correct format: `<provider>/<model-family>@<variant>` — e.g. `google-vertex-anthropic/claude-sonnet-4-6@default`

Wrong formats previously used:
- `claude-sonnet-4-6-20250514` (hyphen-date suffix)
- `claude-sonnet-4-6` (bare, no variant)
- `claude-haiku-4-5-20251001` (hyphen-date suffix)
- `claude-opus-4-6` (bare, no variant)

### Changes

#### 1. Fix model IDs everywhere
Update all three Claude model strings to use `@default` variant suffix.

#### 2. Fix model.json write format
opencode's state file uses `{"recent": [...], "favorite": [], "variant": {...}}` —
not the flat `{"providerID": ..., "modelID": ...}` format that was being written.

#### 3. Model selection for server and TUI modes
Previously the model picker in `sessions/new.html` was hidden inside `#prompt-row`
(invisible when mode ≠ prompt). On `sessions/detail.html` the model selector only
appeared in the `{% if session.mode == 'prompt' %}` block.

Move the picker to be visible for all modes. For server/TUI on the detail page,
include it in the main `cfg-edit-form` (disabled when active, saved with the
existing Save button) so no extra button is needed.

#### 4. Add exec_model_json utility
Add `k8s.exec_model_json(pod_name, namespace, model)` for future use — execs into
a running pod and writes the correct model.json. Not exposed in the UI.

---

## Implementation

### Files changed

#### `swarmer/routers/sessions.py`
- `_CLAUDE_MODELS` list updated:
  - `claude-haiku-4-5-20251001` → `claude-haiku-4-5@20251001` (`@default` does not resolve for Haiku on Vertex)
  - `claude-sonnet-4-6-20250514` → `claude-sonnet-4-6@default`
  - `claude-opus-4-6` → `claude-opus-4-6@default`
- Added `/set-model` POST endpoint (`session_set_model`): saves model to DB and,
  if the pod is running, calls `k8s.exec_model_json` to apply it live. Not wired
  to the UI currently.

#### `swarmer/k8s.py`
- Default model in `_build_opencode_config`: `claude-sonnet-4-6` → `claude-sonnet-4-6@default`
- Added `exec_model_json(pod_name, namespace, model)`: uses `kubernetes.stream.stream()`
  to exec a `sh -c` command into the pod that writes the model.json in opencode's
  expected format (`recent`/`favorite`/`variant`).

#### `swarmer/k8s_session.py`
- Default ADC model: `claude-sonnet-4-6` → `claude-sonnet-4-6@default`
- `model_json` build updated from flat `{"providerID": ..., "modelID": ...}` to:
  ```json
  {
    "recent": [{"providerID": "...", "modelID": "..."}],
    "favorite": [],
    "variant": {"provider/model": "default"}
  }
  ```

#### `swarmer/templates/sessions/new.html`
- Model picker moved outside `#prompt-row` div so it is visible for all modes
  (TUI, server, prompt) at session creation time.

#### `swarmer/templates/sessions/detail.html`
- Model select added inside `cfg-edit-form` for server/TUI modes (same pattern as
  prompt mode: disabled when active, saved via the existing Save button).
- Instruction prompt remains prompt-only; model is now universal.
