# Plan: TUI Patch Export (Optional Feature)
**Date:** 2026-04-28

## Context
Users want to clone a public repo into a TUI session, iteratively work with the agent, and export a `git diff` patch when satisfied — without needing any GitHub token or pull secret. They then download the patch, apply it locally, and push from their own machine.

## Key Constraint
This is **purely additive**. Existing TUI behavior is untouched. Sessions without repos see no changes.

## Approach

### Model changes
- Add `working_branch` (VARCHAR, default `""`), `patch_output` (TEXT, default `""`), and `commit_msg` (TEXT, default `""`) to the `sessions` table.
- Add `ALTER TABLE` migrations in `database.py`.

### Working branch creation
- In `k8s_session.py`, add an optional branch-creation step to the main container command chain.
- Only activates when `session.repos` is non-empty and `session.working_branch` is set.
- Branch name defaults to `swarmer/<session-id>-<hex>` (collision-safe via random suffix).

### Generate / download patch endpoints
- `POST /workspaces/{ws_id}/sessions/{sid}/generate-patch` — `kubectl exec` into the running pod, runs `git diff <original_branch>`, stores result in `session.patch_output`.
- `GET /workspaces/{ws_id}/sessions/{sid}/download-patch` — serves `session.patch_output` as a downloadable `.patch` file.

### UI
- Optional "Working branch" text input on session creation form (visible when a repo might be added later).
- Conditional **Patch** tab on session detail page — only appears when `session.repos` is non-empty.
- Contains: "Generate Patch" button, diff preview, "Download Patch" link.

## Files to Change
- `swarmer/models/session.py` — add `working_branch`, `patch_output`, `commit_msg` columns
- `swarmer/database.py` — add ALTER TABLE migrations
- `swarmer/k8s_session.py` — add branch creation to command chain
- `swarmer/routers/sessions.py` — add `generate-patch`, `download-patch` endpoints; wire `working_branch` in create/edit
- `swarmer/templates/sessions/detail.html` — add Patch tab
- `swarmer/templates/sessions/new.html` — add working branch field

## What Does NOT Change
- TUI sessions without repos: identical behavior, no new UI elements
- Prompt mode, Server mode: untouched
- Git token / pull secret flows: untouched
- Pod lifecycle, log polling, stop/cleanup: unchanged
