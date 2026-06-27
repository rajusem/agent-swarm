## Fix Plan for OBSINTA-1336

### Version
Plan v2 | Iteration 1 (revised after audit)

### Root Cause
Three bugs introduced by the K8s cleanup commit (`4da0039`) that removed the `persist`, `privileged`, `pod_name`, `pvc_name`, and `k8s_secret_names` columns from the Session model without:
1. Adding database migrations to DROP the removed columns from existing SQLite databases
2. Removing references to `persist` from the MCP server client and server code
3. Including all required template context variables in the IntegrityError handler for session creation

**Bug 1 — NOT NULL constraint failed on `sessions.persist`**: The K8s cleanup removed the `persist` column from the SQLAlchemy model but did not add a migration to drop it from existing databases. On existing deployments, the `persist` column still exists with `NOT NULL` and no `server_default`. When SQLAlchemy inserts a new Session row, it doesn't include `persist` (because it's not in the model), and the database rejects the INSERT with a NOT NULL constraint violation. Same applies to `privileged` (NOT NULL, no server_default). The `pod_name`, `pvc_name`, `k8s_secret_names` columns are nullable or have defaults, so they don't cause NOT NULL failures but are still dead columns.

**Bug 2 — MissingGreenlet crash on duplicate session name**: In `routers/sessions.py:session_create()`, the `IntegrityError` handler (line 384) calls `await db.rollback()`. SQLAlchemy always expires all ORM objects on rollback, regardless of `expire_on_commit=False` (which only suppresses expiry on commit). When the handler then accesses `ws.k8s_namespace` (line 394, via `k8s.get_image_available(t.get_image(), ws.k8s_namespace)`), SQLAlchemy attempts a synchronous implicit refresh of the expired `Workspace.namespace` column attribute. In an async context, this triggers a `MissingGreenlet` error.

**Bug 3 — Missing template context in IntegrityError handler**: The IntegrityError handler in `session_create()` renders `sessions/new.html` but omits `mcp_servers` and `prompt_sources` from the template context. The template references `{% if mcp_servers %}` (line 142) and `{% include "sessions/_prompt_picker.html" %}` which iterates over `prompt_sources`. This causes an `UndefinedError` when Jinja2 tries to render the template.

### Approach
Fix all three bugs with minimal changes:
1. Add migrations to drop the removed columns (`persist`, `privileged`, `pod_name`, `pvc_name`, `k8s_secret_names`) from existing databases
2. Fix the IntegrityError handler to re-fetch `ws` from the database BEFORE the `asyncio.gather` call (which accesses `ws.k8s_namespace`) AND include `mcp_servers` and `prompt_sources` in the template context
3. Remove the `persist` parameter from the MCP server client, server code, `_fmt_session()` output, and tool docstrings

### Alternatives Considered
| # | Approach | Pros | Cons | Why Not |
|---|----------|------|------|---------|
| 1 | Add `server_default` to the old columns instead of dropping | Less destructive | Keeps dead columns in the schema, confusing for future developers | Dead code should be removed |
| 2 | Use `selectinload` on workspace before the try block | Pre-loads attributes | Doesn't fix the missing template context (Bug 3); adds coupling | Incomplete fix |
| 3 | Use `db.expire_on_rollback = False` | Prevents expiry | Non-standard, could mask bugs elsewhere | Bad practice |

### Files to Change
| File | Change | Reason |
|------|--------|--------|
| `swarmer/database.py` | Add DROP COLUMN migrations for `persist`, `privileged`, `pod_name`, `pvc_name`, `k8s_secret_names` | Bug 1: Remove leftover NOT NULL columns from existing databases |
| `swarmer/routers/sessions.py` | In `session_create()` IntegrityError handler: re-fetch `ws` BEFORE the `asyncio.gather` call (line 393), then add `mcp_servers` and `prompt_sources` to template context | Bug 2 + Bug 3: Fix MissingGreenlet and missing template variables |
| `mcp-server/agent_swarm_mcp_server/client.py` | Remove `persist` parameter from `create_session()` and its inclusion in the request body | Remove dead code referencing removed column |
| `mcp-server/agent_swarm_mcp_server/server.py` | Remove `persist` parameter from `_create_session()`, `_update_session()`, `_fmt_session()` output dict (line 42), and tool docstrings for create_session/update_session | Remove dead code referencing removed column (addresses ARCH-001/LANG-004 audit finding) |
| `mcp-server/tests/test_server.py` | Remove `persist` from mock session dictionaries | Update tests to match cleaned-up API |
| `mcp-server/tests/conftest.py` | Remove `persist` from fixture data | Update test fixtures |

### Dependencies & Side Effects
- [ ] Public API change? — Yes, minor: `persist` field removed from MCP server create/update session tools and session output. It was already a no-op since the API schema ignores it. `_fmt_session()` will no longer emit `persist` in formatted session dicts.
- [ ] Config / env var change? — No
- [ ] Database migration? — Yes, DROP COLUMN migrations for 5 columns. SQLite supports DROP COLUMN since version 3.35.0 (2021-03-12). The existing migrations already use DROP COLUMN (for `resume`). The `no such column` error suppression in `migrate_db()` makes these idempotent on fresh databases.
- [ ] Downstream consumer impact? — MCP server clients sending `persist` will have it silently ignored (Pydantic v2 default behavior). MCP tool output will no longer include the `persist` field.
- [ ] Error handling / logging change? — Yes, IntegrityError handler improved
- [ ] Performance characteristics change? — No

### Risk Assessment
| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| DROP COLUMN fails on old SQLite | Low | Medium | SQLite 3.35+ supports DROP COLUMN; error handling in `migrate_db()` already suppresses "no such column" errors; existing `DROP COLUMN resume` confirms SQLite version floor |
| MCP clients still sending `persist` | Medium | None | Pydantic v2 silently ignores extra fields by default |
| Template rendering differences after fix | Low | Low | The fix adds the same variables that the happy path includes |

### Test Strategy
- Existing tests to verify: `tests/test_api.py::TestSessionsCRUD::test_create_duplicate_session_name` — verifies the API-level IntegrityError handling (REST endpoint)
- New regression tests needed:
  1. Test that MCP server `create_session` no longer sends `persist` field
  2. Test that `_fmt_session()` output does not contain `persist`

### Confidence
| Dimension | Score | Proof |
|-----------|-------|-------|
| Root cause certainty | HIGH | Git diff of commit `4da0039` shows `persist` column removal without migration; code inspection of IntegrityError handler shows missing template variables and expired ORM objects after rollback |
| Approach correctness | HIGH | Follows existing patterns: `migrate_db()` already uses DROP COLUMN for `resume`; happy-path `session_new()` already includes all required template variables; re-fetch after rollback is idiomatic SQLAlchemy |
| Scope completeness | HIGH | All three bugs traced to specific lines; MCP server cleanup covers `_fmt_session()`, client, server, and docstrings |

### Audit Trail
- **v1 → v2 changes**: Added `_fmt_session()` cleanup (line 42 in server.py) per ARCH-001/LANG-004. Clarified Bug 2 mechanism: `expire_on_commit=False` does NOT protect against rollback expiry — column attribute refresh triggers MissingGreenlet. Specified `ws` re-fetch must happen BEFORE `asyncio.gather` call per PE-001. Updated plan to also clean tool docstrings.
- **Rejected findings**: LANG-001 (CRITICAL) rejected — false positive, git diff proves columns existed; LANG-003 (MAJOR) deferred — form state preservation is UX improvement, not crash fix.

### Investigation Strategy
**Signals detected**: default
**Strategy used**: Standard investigation (grep, file reads, code path tracing, git history analysis)
**Key findings from strategy**:
  - Git history (commit `4da0039`) confirmed the `persist`, `privileged`, `pod_name`, `pvc_name`, `k8s_secret_names` columns were removed from the model without DROP migrations
  - Code path analysis showed the IntegrityError handler at `routers/sessions.py:384-411` is missing `mcp_servers`, `prompt_sources` in template context, and accesses `ws.k8s_namespace` on an expired ORM object after rollback
  - The MCP server code (`client.py:128-138`, `server.py:42,140-155,169-185`) still references the removed `persist` field
