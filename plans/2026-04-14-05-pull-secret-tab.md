# Plan: Pull Secret Tab + Secrets Page Redesign
Date: 2026-04-14

## Goal

Add a per-workspace pull secret (quay.io or any OCI registry) that session pods automatically
reference when pulling images. Consolidate the two separate secrets pages (OpenCode, GitHub PATs)
into a single tabbed page and add a third tab for the pull secret.

## Changes

### 1. `swarmer/models/opencode_secret.py`
- Fixed `masked_api_key` to show `****<last4>` instead of `<first4>...<last4>`.

### 2. `swarmer/k8s.py`
- Added `PULL_SECRET_NAME = "quay-pull-secret"` — fixed convention used across the app.
- Added `apply_pull_secret(namespace, registry, username, password)` — creates or replaces a
  `kubernetes.io/dockerconfigjson` Secret with the provided registry credentials.
- Added `get_pull_secret_info(namespace)` — reads the secret from K8s and returns
  `{"registry": ..., "username": ...}` for display in the UI (password is never returned).
- Added `delete_pull_secret(namespace)` — deletes the secret via `_delete_secret`.

### 3. `swarmer/routers/secrets.py`  (full rewrite)
- Added combined GET `/workspaces/{ws_id}/secrets?tab=<opencode|pats|pull-secret>` route that
  fetches all three data sets and renders a single tabbed template.
- Converted the old per-section GET routes (`/secrets/opencode`, `/secrets/pats`) to 302
  redirects → tabbed page.
- All POST routes (opencode save, PAT create/update/delete) now redirect back to
  `?tab=<appropriate>` instead of the old separate-page URLs.
- Added `POST /workspaces/{ws_id}/secrets/pull-secret` — validates fields, calls
  `k8s.apply_pull_secret`, flash-redirects back to `?tab=pull-secret`.
- Added `POST /workspaces/{ws_id}/secrets/pull-secret/delete` — calls `k8s.delete_pull_secret`.
- Extracted `_secrets_context()` helper to avoid duplicating the three DB/K8s fetches.

### 4. `swarmer/templates/secrets/tabs.html`  (new file)
- Single Bootstrap-tabbed page replacing `opencode_form.html` + `github_pat_list.html` as the
  primary UI (those templates remain for the fallback redirect path).
- OpenCode tab: same two-column form (Vertex AI + Gemini), inline Save & Sync button.
- GitHub PATs tab: full PAT table with Edit/Delete actions; "Add PAT" links to the existing
  separate new-PAT form page.
- Pull Secret tab: registry, username, password fields; shows a success badge with current
  registry/username if the secret already exists in K8s; Delete button when configured.

### 5. `swarmer/templates/secrets/github_pat_form.html`
- Updated breadcrumb and Cancel link from `/secrets/pats` → `/secrets?tab=pats`.

### 6. `swarmer/templates/workspaces/detail.html`
- Changed "Secrets" button href from `/secrets/opencode` → `/secrets` (lands on opencode tab
  by default).

### 7. `swarmer/routers/sessions.py`
- `session_launch` now passes `image_pull_secret=k8s.PULL_SECRET_NAME` instead of
  `settings.agent_image_pull_secret`. Every session pod in a workspace references `quay-pull-secret`;
  K8s silently ignores the reference if the secret doesn't exist and `imagePullPolicy: IfNotPresent`
  is satisfied from the node cache.

## Design decisions

- The pull secret name is fixed (`quay-pull-secret`). One secret per workspace namespace.
  Simplicity over configurability — users who need multiple registries can configure at the
  cluster level.
- Password is never stored in the swarmer DB; it lives only in the K8s Secret. The UI shows
  registry + username on subsequent visits (read from K8s), not the password.
- Session pods always reference `quay-pull-secret`; no per-workspace flag needed. The pod
  spec reference is harmless when the secret is absent (image already cached).
