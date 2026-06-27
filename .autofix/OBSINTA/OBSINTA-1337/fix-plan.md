# Fix Plan for OBSINTA-1337

**Ticket**: [OBSINTA-1337] ACM-35375 x Claude Sonnet 4.6 - Session creation crashes
**Branch**: OBSINTA-1337/acm35375-session-creation-crashes
**Base branch**: issue-fix-ACM-35375
**Confidence**: HIGH
**Status**: Approved (audit skipped — simple fix, all HIGH confidence)

---

## Root Cause

The K8s cleanup commit (`4da0039`) removed five columns from the `Session` model
(`persist`, `privileged`, `pod_name`, `pvc_name`, `k8s_secret_names`) but did not
add corresponding `DROP COLUMN` migrations to `swarmer/database.py`. Any database
created before the cleanup still has those columns with `NOT NULL` constraints and
no `server_default`. When SQLAlchemy tries to INSERT a new session it omits the
removed columns, causing SQLite to raise an `IntegrityError` —
`"NOT NULL constraint failed on sessions.persist"`.

Two additional bugs exist in the `IntegrityError` handler of `session_create`:

1. **MissingGreenlet**: `db.rollback()` expires all ORM objects but the code
   immediately accesses `ws.k8s_namespace` on the now-expired `ws` object without
   re-fetching it. In async SQLAlchemy this raises `MissingGreenlet`.

2. **Missing template context**: The template response for the duplicate-name error
   path is missing `mcp_servers` and `prompt_sources` context variables required by
   `sessions/new.html` (via `_prompt_picker.html`), causing a Jinja2 `UndefinedError`
   (500 response).

**Reference fix**: Commit `de4e40e` (merged to main on June 14 by Joshua Packer)
implements the same three-part fix. This plan applies the same changes to the
`issue-fix-ACM-35375` branch which was forked before that fix landed.

---

## Approach

1. Add five `ALTER TABLE sessions DROP COLUMN` statements to the migration list in
   `swarmer/database.py`, using the existing error-suppression pattern (which already
   handles "no such column" for fresh databases).

2. In the `IntegrityError` handler of `session_create` in `swarmer/routers/sessions.py`:
   - Add `ws = await _get_workspace(ws_id, db)` after `await db.rollback()` to
     re-fetch the workspace with a live ORM session.
   - Add `mcp_servers` and `prompt_sources` to the `TemplateResponse` context dict.

3. Add `tests/test_migrations.py` with three regression tests covering the form
   POST path and migration correctness (gaps the existing API tests miss).

---

## Planned Files

- `swarmer/database.py` — Add 5 `ALTER TABLE sessions DROP COLUMN` statements for
  `persist`, `privileged`, `pod_name`, `pvc_name`, `k8s_secret_names` after the
  `custom_policies` migration entry.

- `swarmer/routers/sessions.py` — In `session_create` IntegrityError handler:
  add `ws = await _get_workspace(ws_id, db)` after `await db.rollback()`, and add
  `"mcp_servers": mcp_servers, "prompt_sources": prompt_sources` to the
  `TemplateResponse` context (after fetching both in the handler).

- `tests/test_migrations.py` — New file with:
  - `TestMigrateDbDropsLegacyColumns.test_persist_column_dropped_by_migration`
  - `TestMigrateDbDropsLegacyColumns.test_migrate_db_idempotent_on_fresh_schema`
  - `TestSessionFormCreatePath.test_form_create_duplicate_name_returns_422`

---

## Exact Code Changes

### `swarmer/database.py`

After the line:
```python
        "ALTER TABLE sessions ADD COLUMN custom_policies TEXT NOT NULL DEFAULT ''",
```

Add:
```python
        # ACM-35375: drop columns removed from Session model in ACM-34863 (K8s cleanup)
        # Error suppression ("no such column") handles fresh databases safely.
        "ALTER TABLE sessions DROP COLUMN persist",
        "ALTER TABLE sessions DROP COLUMN privileged",
        "ALTER TABLE sessions DROP COLUMN pod_name",
        "ALTER TABLE sessions DROP COLUMN pvc_name",
        "ALTER TABLE sessions DROP COLUMN k8s_secret_names",
```

### `swarmer/routers/sessions.py`

In `session_create`, find the `except IntegrityError:` block:
```python
    except IntegrityError:
        await db.rollback()
        pats = await _visible_pats(ws_id, db, user_id=_current_user(request))
```

Change to:
```python
    except IntegrityError:
        await db.rollback()
        # Rollback expires all ORM objects; re-fetch ws so the template can read
        # ws.k8s_namespace without triggering a lazy-load in async context (MissingGreenlet).
        ws = await _get_workspace(ws_id, db)
        pats = await _visible_pats(ws_id, db, user_id=_current_user(request))
```

Then in the `TemplateResponse` call in the same handler, after:
```python
                "tool_image_available": dict(zip([t.name for t in _tools], _avail, strict=False)),
```

Add (and fetch the values above the return):
```python
        from swarmer.routers.mcp_servers import get_enabled_mcp_servers
        mcp_servers = await get_enabled_mcp_servers(ws_id, db, user_id=_current_user(request))
        prompt_sources = await _get_prompt_sources(ws_id, db)
```

And add to the template context:
```python
                "mcp_servers": mcp_servers,
                "prompt_sources": prompt_sources,
```

### `tests/test_migrations.py`

New file — see reference implementation in commit `de4e40e` on `main`:
```
tests/test_migrations.py  (242 lines)
```
The test file must cover:
- Injecting legacy `persist NOT NULL` column, running `migrate_db()`, verifying DROP
- Running `migrate_db()` on fresh schema without error
- Submitting duplicate session name via form POST, expecting 422 (not 500)

---

## Alternatives Considered

| # | Approach | Pros | Cons | Why Not |
|---|----------|------|------|---------|
| 1 | Add `server_default` back to removed columns in model | Avoids migration | Contradicts cleanup goal; resurrects deleted columns | Defeats ACM-34863 |
| 2 | `expire_on_commit=False` globally | Prevents all expired-object access | Hides real bugs; stale data risk | Already set; rollback still expires; not the right fix |

---

## Test Strategy

Run the new tests:
```bash
pytest tests/test_migrations.py -v
```

Run existing tests to verify no regressions:
```bash
pytest tests/test_api.py tests/test_openshell_session.py -v
```

---

## Audit Trail

- **Architecture**: approved (simple, self-contained) — skipped (complexity gate rule 5)
- **PE**: approved (migration is idempotent, no deployment risk) — skipped
- **Language**: approved (Python/SQLAlchemy patterns correct) — skipped
- **Audit skipped**: simple fix, all HIGH confidence, ≤4 files, regression with clear root cause
- **Reference validation**: approach confirmed by merged fix `de4e40e` (Joshua Packer, June 14, 2026)
