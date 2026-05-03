# Plan: GitHub Repository Picker for Session Git Repositories

**Date:** 2026-05-03  
**Status:** Implemented

---

## Goal

Replace the manual URL text-input for adding Git repositories on the Session detail page with a GitHub-API-backed repository picker. When a GitHub PAT is selected, clicking "+" lists all accessible repositories for that PAT (personal and, optionally, an enterprise org) and lets the user pick one. A manual URL fallback is kept for non-GitHub repos or edge cases.

---

## Background & Current State

The "Git Repositories" card on `sessions/detail.html` currently works like this:

1. A PAT selector (`_pat_select.html`) lets the user choose a GitHub PAT for auth.
2. Clicking "+" (`repo-add-btn`) reveals an inline form (`_repo_list.html`, lines 54–85) with three text inputs: `repo_url` (pre-filled with `https://github.com/`), `branch`, and `local_path`.
3. The form POSTs to `/workspaces/{ws_id}/sessions/{sid}/repos`, which creates a `SessionRepo` row in SQLite.
4. After add/delete, the `_repo_list.html` HTMX partial is re-rendered.

The `GitHubPAT` model (`models/github_pat.py`) stores:
- `github_username` — the user's GitHub login (already present)
- (missing) **`github_org`** — optional enterprise org to list repos from

The GitHub REST API supports:
- `GET /user/repos` — list repos for the authenticated user
- `GET /orgs/{org}/repos` — list repos for an org

---

## User-Confirmed Decisions

| Decision | Choice |
|---|---|
| Org association | Store `github_org` field on PAT; when set, list org repos; when blank, list user repos |
| Picker UX | Inline HTMX-fetched searchable list (no modal) |
| Manual URL fallback | Keep as an "Enter URL manually" toggle below the picker |
| No PAT selected | Show a hint: "Select a GitHub PAT above to browse repositories" |

---

## Implementation Plan

### 1. Database: Add `github_org` to `GitHubPAT`

**File:** `swarmer/models/github_pat.py`

- Add `github_org: Mapped[str] = mapped_column(Text, nullable=False, default="")` column.
- This is optional — when blank, repos are listed for `github_username`.

**File:** `swarmer/database.py`

- Add a migration in `migrate_db()`:
  ```python
  "ALTER TABLE github_pats ADD COLUMN github_org TEXT NOT NULL DEFAULT ''",
  ```

---

### 2. PAT Form: Add `github_org` Field

**File:** `swarmer/templates/secrets/github_pat_form.html`

Add a new optional form field after the existing "GitHub Username" field:

```text
GitHub Org (optional)
  <input name="github_org" placeholder="my-enterprise-org">
  Helper text: "Leave blank to list repos from your personal account.
                Set to an org name to list repos from that org instead."
```

**File:** `swarmer/routers/secrets.py`

- In `github_pat_create` (POST `/workspaces/{ws_id}/secrets/pats`):
  - Accept `github_org: str = Form("")`
  - Set `pat.github_org = github_org.strip()`
- In `github_pat_update` (POST `/workspaces/{ws_id}/secrets/pats/{pat_id}/edit`):
  - Same — accept and save `github_org`.

---

### 3. New Backend Endpoint: List Repos for a PAT

**File:** `swarmer/routers/sessions.py`

Add a new HTMX endpoint:

```text
GET /workspaces/{ws_id}/sessions/{sid}/repos/pick
    ?pat_id={pat_id}
```

**Logic:**
1. Look up the `GitHubPAT` by `pat_id` (verify `workspace_id` matches).
2. Determine the scope:
   - If `pat.github_org` is set → call `GET /orgs/{pat.github_org}/repos?per_page=100&sort=updated`
   - Else → call `GET /user/repos?per_page=100&sort=updated&affiliation=owner,collaborator`
3. Paginate through all pages (follow GitHub's `Link: <next>` header) up to a reasonable cap (e.g. 500 repos).
4. Return the list sorted by `updated_at` descending.
5. Render the new `sessions/_repo_picker.html` HTMX partial.

**Error handling:**
- PAT not found or wrong workspace → return empty partial with error message.
- GitHub API error (bad token, network) → return inline error message in the partial.

---

### 4. New Template: `_repo_picker.html`

**File:** `swarmer/templates/sessions/_repo_picker.html`  
_(New HTMX partial, returned by the endpoint above)_

Structure:
```text
┌─────────────────────────────────────────────────────┐
│ [🔍 Filter repos...          ] (search input, JS)    │
│                                                       │
│ owner/repo-one          (Updated 2 days ago)         │
│ owner/repo-two          (Updated 5 days ago)         │
│ ...                                                   │
│                                                       │
│ [Enter URL manually ▸]   [Cancel]                    │
└─────────────────────────────────────────────────────┘
```

Behavior:
- Each repo row is a clickable item. Clicking a repo reveals a branch/path confirmation mini-form inline.
- The filter input uses a small `oninput` JS snippet to hide/show rows client-side (no round-trip needed for filtering).
- "Enter URL manually" toggles back to the existing manual URL form.
- "Cancel" hides the picker and restores the "+" button.

When a repo is selected, a secondary mini-form appears inline:
```text
Branch: [main        ]
Local path: [repo-name   ]
[Add]  [Back]
```
This submits to the existing `POST /workspaces/{ws_id}/sessions/{sid}/repos` endpoint — no change to the add handler.

---

### 5. Modify `_repo_list.html`: Replace "+" Inline Form with Picker Trigger

**File:** `swarmer/templates/sessions/_repo_list.html`

Replace the current `onclick` on `#repo-add-btn` from toggling the inline form to making an HTMX request when a PAT is selected. When no PAT is selected, show inline hint instead.

The existing manual-entry inline form markup is kept but initially hidden, shown only when "Enter URL manually" is clicked from within `_repo_picker.html`.

---

### 6. GitHub API Helper: `_list_repos_for_pat()`

**File:** `swarmer/routers/sessions.py`

New async helper alongside the existing `_fetch_repo_info()`:

```python
async def _list_repos_for_pat(pat: GitHubPAT) -> list[dict] | str:
    """
    Returns a list of repo dicts [{full_name, updated_at, private}, ...]
    or a string error message on failure.
    Fetches all pages up to 500 repos.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"token {pat.pat}",
    }
    repos = []
    if pat.github_org:
        url = f"https://api.github.com/orgs/{pat.github_org}/repos"
        params = {"per_page": 100, "sort": "updated"}
    else:
        url = "https://api.github.com/user/repos"
        params = {"per_page": 100, "sort": "updated", "affiliation": "owner,collaborator"}

    async with httpx.AsyncClient(timeout=10) as client:
        while url and len(repos) < 500:
            r = await client.get(url, headers=headers, params=params)
            params = {}  # params only on first request; next URL has them baked in
            if r.status_code != 200:
                return f"GitHub API error {r.status_code}: {r.json().get('message', 'unknown')}"
            repos.extend(r.json())
            # Follow Link header pagination
            link = r.headers.get("link", "")
            next_url = None
            for part in link.split(","):
                if 'rel="next"' in part:
                    next_url = part.split(";")[0].strip().strip("<>")
            url = next_url

    return repos
```

---

## File Change Summary

| File | Change Type | Description |
|---|---|---|
| `swarmer/models/github_pat.py` | Edit | Add `github_org` column |
| `swarmer/database.py` | Edit | Add `ALTER TABLE github_pats ADD COLUMN github_org` migration |
| `swarmer/templates/secrets/github_pat_form.html` | Edit | Add `github_org` input field |
| `swarmer/routers/secrets.py` | Edit | Accept & save `github_org` in create/update PAT handlers |
| `swarmer/routers/sessions.py` | Edit | Add `_list_repos_for_pat()` helper + `GET .../repos/pick` route |
| `swarmer/templates/sessions/_repo_list.html` | Edit | Replace inline add form trigger with HTMX picker trigger; keep manual form as hidden fallback |
| `swarmer/templates/sessions/_repo_picker.html` | **New** | HTMX partial: searchable repo list, repo selection mini-form, manual URL toggle, cancel |

---

## Data Flow

```text
User clicks "+"
    │
    ├─ No PAT selected → show inline hint text (no API call)
    │
    └─ PAT selected
           │
           └─ HTMX GET /repos/pick?pat_id=N
                  │
                  ├─ pat.github_org set → GET /orgs/{org}/repos (paginated)
                  └─ pat.github_org blank → GET /user/repos (paginated)
                         │
                         └─ Render _repo_picker.html
                                │
                                ├─ User filters list (JS, client-side)
                                ├─ User clicks a repo → branch/path mini-form appears
                                ├─ User submits → POST /repos (existing handler, no change)
                                └─ "Enter URL manually" → show original text-input form
```

---

## Considerations & Edge Cases

- **Large org repos**: Pagination cap at 500 repos prevents runaway API calls. A note is shown if the list was truncated.
- **PAT change mid-session**: The repo picker is triggered fresh on each "+" click, so switching PATs and clicking "+" again re-fetches with the new PAT.
- **Active session**: The "+" button is disabled when `session.is_active` — consistent with existing repo delete button behavior.
- **Non-GitHub repos**: Manual URL fallback ensures users can still add GitLab, Bitbucket, or internal git servers.
- **Rate limiting**: With a PAT the limit is 5,000 req/hr. The paginated fetch uses at most 5 API calls per picker open — well within limits.
- **`github_org` on existing PATs**: Migration adds the column with `DEFAULT ''`, so existing PATs default to listing user repos — no breaking change.

---

## Implementation Notes

### Actual files changed

| File | Change Type | Description |
|---|---|---|
| `swarmer/github.py` | **New** | Extracted GitHub API helpers (`github_slug`, `fetch_repo_info`, `list_repos_for_pat`) into a standalone module with no FastAPI/SQLAlchemy deps — enables isolated unit testing |
| `swarmer/models/github_pat.py` | Edit | Added `github_org: Mapped[str]` column (line 20) |
| `swarmer/database.py` | Edit | Added `ALTER TABLE github_pats ADD COLUMN github_org TEXT NOT NULL DEFAULT ''` migration |
| `swarmer/templates/secrets/github_pat_form.html` | Edit | Added "GitHub Org (optional)" form field between Username and Token fields |
| `swarmer/routers/secrets.py` | Edit | `github_pat_create` and `github_pat_update` now accept and persist `github_org: str = Form("")`; PAT update `db.commit()` wrapped in `IntegrityError` handler |
| `swarmer/routers/sessions.py` | Edit | Removed inline helper functions (moved to `swarmer/github.py`); imports via `from swarmer.github import ...`; added `GET .../repos/pick` route (`repo_pick`) and `GET .../repos/items` route (`repo_items`); `is_active` guard added to `repo_add`, `repo_delete`, `repo_pick`; `repo_add` and `repo_delete` return empty 200 with `HX-Trigger: repoListChanged` |
| `swarmer/templates/sessions/_repo_picker.html` | **New** | HTMX partial: client-side filterable repo list, click-to-select with branch/path confirmation, "Enter URL manually" fallback, cancel; escapes `full_name`/`description`; populates branch from `repo.default_branch`; forms use `hx-swap="none"` (refresh driven by `HX-Trigger`) |
| `swarmer/templates/sessions/_repo_list.html` | Edit | Stable shell (`<div id="repo-list">`) with `hx-get="/repos/items"` + `hx-trigger="repoListChanged from:body"` — fetches fresh inner content whenever the event fires; includes `_repo_items.html` at initial page render |
| `swarmer/templates/sessions/_repo_items.html` | **New** | Inner partial: repo rows, delete buttons (`hx-swap="none"`), three-state add button; served by `GET /repos/items` and swapped into `#repo-list` via `innerHTML` on `repoListChanged` |
| `swarmer/templates/sessions/detail.html` | Edit | `#repo-picker-container` placed as a sibling of `#repo-list` inside the card body |
| `tests/test_list_repos_for_pat.py` | **New** | 8 unit tests for `list_repos_for_pat` using `respx` to mock httpx — no live network required |

### Deviations from plan

**GitHub helpers module**: The plan called for `_list_repos_for_pat()` to live in `swarmer/routers/sessions.py` alongside `_fetch_repo_info()`. During implementation, all three GitHub helpers were moved to a new `swarmer/github.py` module so they could be imported by the test suite without pulling in FastAPI/SQLAlchemy. The sessions router imports them via aliases (`_fetch_repo_info`, `_list_repos_for_pat`) preserving all existing call sites unchanged.

**Card refresh bug (three iterations)**: Getting the repo list to refresh after add/delete took three attempts.

*Iteration 1* — original implementation used `hx-swap="outerHTML"` + `hx-select="#repo-list"` on the add/delete response. Silently failed in HTMX 1.9 when the triggering element was a descendant of the swap target (removed from the DOM mid-swap).

*Iteration 2* — split the template into a stable `_repo_list.html` shell and an `_repo_items.html` inner partial; switched to `hx-swap="innerHTML"` on `#repo-list`. The swap strategy was correct in principle but still did not reliably update the UI in the browser.

*Iteration 3 (final)* — decoupled the write from the read using `HX-Trigger`:
- `repo_add` and `repo_delete` return an empty 200 with `HX-Trigger: repoListChanged`; they do no DOM swap themselves
- A new `GET /repos/items` endpoint returns `_repo_items.html` as a pure read
- The `#repo-list` shell listens for `repoListChanged from:body` and fetches `/repos/items`, swapping its own `innerHTML`
- Delete buttons and picker forms all use `hx-swap="none"` — writes and reads are fully decoupled

This pattern (write triggers event → stable element self-refreshes via GET) is robust across all HTMX 1.9 edge cases because the element doing the swap (`#repo-list`) is never the one initiating the triggering request.

### Test results

```text
8 passed in 0.20s
```

Tests cover: single-page user repos, org repos, multi-page pagination, API errors (401), network errors, empty list, 500-repo cap enforcement, and Authorization header verification.
