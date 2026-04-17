# Plan: Fix repo-add 500 — SQLAlchemy async lazy-load crash
**Date:** 2026-04-17
**Branch:** main

## Context
Adding the first git repository to a newly created session returned HTTP 500. The page never refreshed to show the new entry. The server log showed:

```
sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called; can't call await_only() here.
```

Root cause: in `repo_add`, after committing the new `SessionRepo`, the code called `db.get(Session, sid, options=[selectinload(...)])` a second time to reload the session with its relationships. In SQLAlchemy async, `session.get()` can return the already-expired identity-map instance without re-executing the query, leaving `github_pat` in a lazy (unloaded) state. Accessing `session.github_pat.pat` then triggered a synchronous lazy-load, which is forbidden in an async context.

## Approach
Replace the second `db.get(...)` call with an explicit `db.execute(select(Session).where(...).options(...))`. An explicit `select` always issues a fresh query and applies the specified `selectinload` options, so all relationships are eagerly loaded before any attribute access.

Also remove the now-redundant `await db.refresh(session)` that was called before the second `db.get`.

## Files to Change
- `swarmer/routers/sessions.py` — replace `db.get` + `db.refresh` pattern in `repo_add` with `db.execute(select(...).options(...))`

## Verification
1. Start the app: `uvicorn swarmer.main:app --reload`
2. Create a new session, navigate to its detail page.
3. Add a git repository via the Add form.
4. Confirm: HTTP 200 returned, repo row appears in the table immediately without a full page reload.
5. Add a second repo — confirm it also appears.

---
## Implementation Summary
**Completed:** 2026-04-17

### What Changed
- `swarmer/routers/sessions.py` (`repo_add`) — replaced `db.get(Session, sid, options=[...])` + `db.refresh(session)` with `db.execute(select(Session).where(Session.id == sid).options(selectinload(Session.repos), selectinload(Session.github_pat)))`. This guarantees a fresh SQL query with eager loads applied, avoiding the identity-map cache hit that left `github_pat` in a lazy state.

### Tests
- No automated tests added — manual verification via the UI (add repo to a new session, confirm row appears without page reload).

### Known Gaps / Follow-up
- The same `db.get` + relationship access pattern exists in `repo_delete` (line ~754); it works today only because `repo_delete` doesn't access `github_pat` inline — worth auditing for consistency.
- No integration tests for the repo add/delete HTMX endpoints.
