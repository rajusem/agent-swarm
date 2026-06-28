## Fix Plan for OBSINTA-1339

### Version
Plan v1 | Iteration 0 (initial draft)

### Root Cause
K8s cleanup commit (4702d7e) removed five columns (`persist`, `privileged`, `pod_name`, `pvc_name`, `k8s_secret_names`) from the Session model but did NOT add corresponding database migrations. When existing sessions are loaded, the ORM objects contain these fields but the database table lacks the columns — causing NOT NULL constraint failures. Additionally, the IntegrityError handler after rollback uses an expired ORM object (`ws`) to access `ws.k8s_namespace`, triggering MissingGreenlet in async context. The handler also lacks `mcp_servers` and `prompt_sources` in the template context.

### Approach
Add DROP COLUMN migrations to remove the five unused columns from the database, and fix the IntegrityError handler to re-fetch workspace and include missing template context variables.

### Alternatives Considered
| # | Approach | Pros | Cons | Why Not |
|---|----------|------|------|---------|
| 1 | Ignore the dead columns | No code changes | Columns remain in DB, potential confusion | Does not fix the root cause |
| 2 | Add migrations + fix handler | Fixes all three bugs | Minimal code changes | **Selected** |
| 3 | Recreate database | Clean slate | Destroys existing data, not viable | Excessive |

### Files to Change
| File | Change | Reason |
|------|--------|--------|
| `swarmer/database.py` | Add 5 DROP COLUMN migrations for removed Session fields | Fixes NOT NULL constraint failures |
| `swarmer/routers/sessions.py` | Re-fetch workspace after rollback, add missing template context | Fixes MissingGreenlet crash and template errors |
| `mcp-server/agent_swarm_mcp_server/client.py` | Remove `persist` parameter from create_session | Removes dead code reference |
| `mcp-server/agent_swarm_mcp_server/server.py` | Remove `persist` from `_fmt_session` and session methods | Removes dead code reference |

### Dependencies & Side Effects
- [ ] Public API change? **No** — internal fixes only
- [ ] Config / env var change? **No**
- [ ] Database migration? **Yes** — DROP COLUMN for 5 removed fields
- [ ] Downstream consumer impact? **No**
- [ ] Error handling / logging change? **Yes** — fixes crash in error path
- [ ] Performance characteristics change? **No**

### Risk Assessment
| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Migration fails on existing DB | LOW | MEDIUM | SQLite ignores "no such column" errors; migrations already handle "duplicate column" |
| Template context still missing items | LOW | LOW | Only 2 variables added; verified against template usage |

### Test Strategy
- Existing tests to verify: Run test suite (`make test`)
- New regression test: Verify `persist` is not sent to API or included in session output

### Confidence
| Dimension | Score | Proof |
|-----------|-------|-------|
| Root cause certainty | HIGH | Git history shows commit 4702d7e removed columns without migrations; IntegrityError handler clearly uses expired ORM |
| Approach correctness | HIGH | Fix directly addresses each of the 3 bugs mentioned in the ticket |
| Scope completeness | HIGH | All 5 dead columns addressed; both error handlers fixed; dead references removed |

### Investigation Strategy
**Signals detected**: regression (bugs introduced by K8s cleanup)
**Strategy used**: Standard investigation — traced code path from session creation to database error
**Key findings from strategy**:
- Commit 4702d7e ("chore(cleanup): remove all legacy K8s pod/secret/PVC management code") removed model fields but forgot migrations
- The IntegrityError handler at line 384 in sessions.py uses `ws.k8s_namespace` after rollback
- Template `sessions/new.html` requires `mcp_servers` and `prompt_sources` which were not passed on error