# Fix: GitHub PAT Form Action URL (405 Method Not Allowed)

**Date:** 2026-04-15

## Problem

Saving a new GitHub PAT secret via the UI returned a `405 Method Not Allowed` from the server:

```
POST /workspaces/1/secrets?tab=pats/ HTTP/1.1  →  405
```

## Investigation

**Entry point:** The 405 response URL `POST /workspaces/1/secrets?tab=pats/` told the story immediately — the path segment (`pats/`) was landing in the query string, not the URL path. This meant the request was hitting `POST /workspaces/{ws_id}/secrets`, which has no POST handler (only a GET), hence 405.

**Files checked:**

1. `swarmer/routers/secrets.py` — confirmed the correct POST routes exist:
   - `POST /workspaces/{ws_id}/secrets/pats` — create a new PAT (line 171)
   - `POST /workspaces/{ws_id}/secrets/pats/{pat_id}/edit` — update an existing PAT (line 238)

2. `swarmer/templates/secrets/github_pat_form.html` — found the bug on line 24:

```html
<!-- BEFORE (broken) -->
action="/workspaces/{{ ws.id }}/secrets?tab=pats/{{ pat.id + '/edit' if pat else '' }}"
```

The Jinja expression was embedded inside a `?tab=pats/...` query parameter rather than as a URL path. For new PATs (`pat` is `None`), this produced:

```
/workspaces/1/secrets?tab=pats/
```

For edits it produced:

```
/workspaces/1/secrets?tab=pats/42/edit
```

Neither matches any registered POST route.

## Fix

**File:** `swarmer/templates/secrets/github_pat_form.html`, line 24

```html
<!-- AFTER (fixed) -->
action="/workspaces/{{ ws.id }}/secrets/pats/{{ pat.id|string + '/edit' if pat else '' }}"
```

This produces the correct POST targets:

| Case     | URL                                      | Matching route                                      |
|----------|------------------------------------------|-----------------------------------------------------|
| New PAT  | `POST /workspaces/1/secrets/pats/`       | `github_pat_create` (secrets.py:171)                |
| Edit PAT | `POST /workspaces/1/secrets/pats/42/edit`| `github_pat_update` (secrets.py:238)                |

Also added the `|string` Jinja filter to `pat.id` to ensure the integer ID is safely concatenated with the `'/edit'` string suffix.
