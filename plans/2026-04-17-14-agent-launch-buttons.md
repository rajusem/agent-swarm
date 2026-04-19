# Feature: Agent Launch Buttons

**Date:** 2026-04-17

## Problem

The session detail page had two separate dropdowns — "Agent Tool" and "Agent Image" — that were disconnected from the actual launch action. The user had to:
1. Pick a tool from a dropdown
2. Pick an image from a second dropdown
3. Click a separate Launch button

This was redundant: the tool already determines the image. There was also no inline feedback on which tools were ready to use without squinting at ✓/✗ indicators below a separate select.

## Plan

1. Remove the "Agent Tool" select from the config card.
2. Remove the "Agent Image" select (and its ✓/✗ indicators) from the config card.
3. Add an "Agent Launch:" row with one green/grey button per registered tool.
   - Green (`btn-success`) if the tool's image is configured (non-empty string).
   - Grey/disabled (`btn-secondary`) if the image is not set.
4. Clicking a launch button POSTs `agent_tool` directly to the `/launch` endpoint, updating the session's stored tool and starting the pod in one step.
5. While the session is active, replace the three buttons with a single orange **Stop** button in the same row.
6. When a prompt-mode session succeeds, relabel the Stop button to **↺ Clean up** (matching the existing behaviour of the top-bar Stop button).
7. Add a third registered tool — **OpenCode (Python)** — backed by `AGENT_IMAGE_PYTHON`.
8. Fix startup crash: pydantic `Settings` rejected unknown env vars (e.g. `AGENT_IMAGE_PYTHON`) because `extra` was not set to `"ignore"`.

## Implementation

### `swarmer/config.py`
- Added `"extra": "ignore"` to `model_config` so unknown env vars no longer cause a `ValidationError` at startup.
- Added `agent_image_python: str = ""` field.
- Updated `agent_image_opencode` default to match the golang image default.

### `swarmer/agent_tools/opencode.py`
- `get_image()` now returns `settings.agent_image_opencode` (was `settings.agent_image`).
- `display_name` changed to `"OpenCode (Golang)"` to distinguish it from the Python variant.

### `swarmer/agent_tools/opencode_python.py` *(new)*
`PythonStrategy` subclasses `OpenCodeStrategy` and overrides:
- `name` → `"opencode-python"`
- `display_name` → `"OpenCode (Python)"`
- `get_image()` → `settings.agent_image_python`

All other behaviour (config map, secret, K8s volumes, model options) is inherited unchanged.

### `swarmer/agent_tools/registry.py`
- Registered `PythonStrategy()` between `OpenCodeStrategy` and `CrushStrategy` so the button order is Golang → Python → Crush.

### `swarmer/routers/sessions.py`
- `session_launch`: added `agent_tool: str = Form("")` parameter. If supplied and valid, the session's `agent_tool` is updated before the pod is built, so the launch button is the single point of tool selection.
- `session_detail`: added `tool_image_available` to template context — a `dict[str, bool]` keyed by tool name, `True` when `tool.get_image()` is non-empty.

### `swarmer/templates/sessions/detail.html`
- Removed "Agent Tool" `<select>` row.
- Removed "Agent Image" `<select>` row with `language_options` / `image_status`.
- Added "Agent Launch:" row containing:
  - `#launch-btns`: flex row of per-tool `<form>` + `<button>` elements; hidden when session is active.
  - `#launch-stop`: a single orange Stop `<form>` + `<button id="launch-stop-btn">`; hidden when session is idle.
- Extended `htmx:afterSwap` JS handler:
  - Toggles `launch-btns` / `launch-stop` visibility in sync with the polled session phase.
  - Relabels `#launch-stop-btn` to "↺ Clean up" when a prompt session reaches the succeeded phase (mirrors the existing top-bar stop-button behaviour).

### `.env` / `.env.example`
- Added `AGENT_IMAGE_OPENCODE` and `AGENT_IMAGE_CRUSH` to `.env` (were missing after the Crush merge).
- Added `AGENT_IMAGE_CRUSH` and `AGENT_IMAGE_OPENCODE` to `.env.example` for documentation.

### `swarmer/agent_tools/crush.py` *(post-merge fix)*
- Corrected four label strings where `"vertexai/gemini-3-pro"` and `"vertexai/gemini-3-flash"` were labelled `"Gemini 2.5 Pro"` / `"Gemini 2.5 Flash (fast)"` instead of `"Gemini 3 Pro"` / `"Gemini 3 Flash (fast)"`. Both the `has_adc` and `has_vertex` option lists were affected.

### `swarmer/routers/tui_ws.py` *(post-merge security fix)*
- `session.model` and other elements of `tui_cmd_parts` were joined with a bare `" ".join(...)` and interpolated into a `sh -c` string, allowing shell injection via a crafted model name.
- Added `import shlex` and changed the join to `" ".join(shlex.quote(p) for p in tui_cmd_parts)` so every argv element is safely quoted before being embedded in the shell command.

### `swarmer/routers/sessions.py` *(tool_image_available fix)*
- The `tool_image_available` dict was built by calling `k8s.get_image_available()`, which does a live HTTP manifest check against the registry. `quay-pull-secret` only has Quay credentials, so private GHCR images (`anomalyco/opencode`) always failed authentication and returned `False`, even though K8s can pull them fine. Public GHCR images (Crush) happened to work anonymously, creating an inconsistent result.
- Changed to `{t.name: bool(t.get_image()) for t in all_tools()}` — green means the image is configured (non-empty string), matching the original plan's intent.

### `swarmer/k8s.py` *(connection-refused fix)*
- `get_pod_status` only caught `kubernetes.client.exceptions.ApiException`, so a `MaxRetryError` / `ConnectionRefusedError` (K8s API server unreachable) propagated as an unhandled exception and returned HTTP 500 on the session detail page.
- Added a bare `except Exception` fallback that returns `("pending", "Cluster Unavailable")`, matching the graceful-degradation pattern already used for 404 responses.

## Verification

1. Start the server — no `ValidationError` on startup even with `AGENT_IMAGE_PYTHON` set.
2. Open a session detail page — "Agent Launch:" row shows three buttons: **OpenCode (Golang)** (green), **OpenCode (Python)** (green if `AGENT_IMAGE_PYTHON` set, grey if not), **Crush** (green).
3. Click a launch button — the session starts using that tool's image; buttons are replaced by an orange Stop button.
4. Click Stop — session clears; three launch buttons reappear.
5. In prompt mode, wait for the session to complete — Stop button relabels to "↺ Clean up".
6. Confirm `kubectl get pod <name> -o jsonpath='{.spec.containers[0].image}'` matches the chosen tool's image.
