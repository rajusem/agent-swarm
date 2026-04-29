# Feature: Per-Session Language / Agent Image Selector

**Date:** 2026-04-15
**PR:** [#4 feat: add Crush CLI agent tool support](https://github.com/stolostron/agent-swarm/pull/4)

## Problem

The agent image (`opencode-golang:latest`) was a single global setting applied to every session. There was no way to choose between `opencode-golang` and `opencode-python` per session, and no visibility into whether either image was actually reachable in the registry before launching.

## Plan

1. Add `language` field to `Session` model (values: `"golang"`, `"python"`)
2. Add `image_for_language(lang)` helper to `Settings` to resolve the full image name
3. Add `check_image_reachable(image, namespace)` async function in `k8s.py` using the pull secret credentials and the registry v2 manifest API
4. Thread `image_status` (per-language reachability) through the new-session and detail routes
5. Add a language dropdown with ✓/✗ indicators to both the new session form and the detail/edit config card
6. At launch time, use `settings.image_for_language(session.language)` instead of `settings.agent_image`

## Implementation

### `swarmer/config.py`
Added `agent_image_python: str = ""` setting and `image_for_language(language)` method:
- For `"golang"`: returns `agent_image`
- For `"python"`: returns `agent_image_python` if set, otherwise derives it by replacing `"golang"` → `"python"` in `agent_image`
- Added `LANGUAGE_OPTIONS = ("golang", "python")` constant

### `swarmer/models/session.py`
Added:
```python
language: Mapped[str] = mapped_column(String(32), nullable=False, default="golang", server_default="golang")
```

### `swarmer/database.py`
Added migration to `migrate_db()`:
```python
"ALTER TABLE sessions ADD COLUMN language VARCHAR(32) NOT NULL DEFAULT 'golang'"
```

### `swarmer/k8s.py`
Added `async def check_image_reachable(image: str, namespace: str) -> bool`:
- Parses image into `registry`, `repo`, `tag`
- Reads `.dockerconfigjson` from the workspace pull secret (`quay-pull-secret`)
- Makes an authenticated GET to `https://{registry}/v2/{repo}/manifests/{tag}`
- Handles Bearer token challenge (WWW-Authenticate → fetch token → retry)
- Returns `True` on HTTP 200, `False` on anything else or exception
- Timeout: 5 seconds

### `swarmer/routers/sessions.py`
- Added `_image_status(namespace)` helper: runs `check_image_reachable` for all language options in parallel via `asyncio.gather`
- `session_new` GET: passes `image_status`, `language_options` to template
- `session_create` POST: accepts `language: str = Form("golang")`, validates against `LANGUAGE_OPTIONS`, stores on `Session`
- `session_detail` GET: passes `image_status`, `language_options` to template
- `session_edit` POST: accepts `language: str = Form("golang")`, updates `session.language`
- `session_launch` POST: uses `settings.image_for_language(session.language)` instead of `settings.agent_image`

### `swarmer/templates/sessions/new.html`
Added "Agent Image" card (right column, above Mode):
```html
<select name="language">
  <option value="golang">opencode-golang</option>
  <option value="python">opencode-python</option>
</select>
<!-- ✓ golang  ✗ python  (green/red per reachability) -->
```

### `swarmer/templates/sessions/detail.html`
Added "Agent Image" row to the config card (below Mode, above GitHub PAT):
- Same dropdown as new.html
- Disabled while session is active (consistent with Mode/PAT fields)
- ✓/✗ indicators shown as form-text below the select

## Verification
1. Open New Session — Agent Image card shows dropdown with ✓/✗ next to each language
2. Create a session with `python` — on launch, the pod spec uses the python image
3. `kubectl get pod <name> -o jsonpath='{.spec.containers[0].image}'` — confirms correct image
4. Detail page shows the saved language and allows switching when session is idle
5. With no pull secret in the namespace — both indicators show ✗
