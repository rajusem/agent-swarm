## Fix Plan for OBSINTA-1339

### Root Cause
K8s cleanup commit (4702d7e) removed five columns (`persist`, `privileged`, `pod_name`, `pvc_name`, `k8s_secret_names`) from the Session model but did NOT add database migrations. This causes NOT NULL constraint failures on existing databases. Additionally, the IntegrityError handler in `session_create` uses an expired ORM object after rollback (accessing `ws.k8s_namespace` triggers MissingGreenlet in async context), and lacks required template context variables (`mcp_servers`, `prompt_sources`).

### Approach
1. Add DROP COLUMN migrations to `database.py` for the 5 removed columns
2. Fix the IntegrityError handler in `sessions.py` to re-fetch workspace after rollback
3. Add missing template context variables (`mcp_servers`, `prompt_sources`) in the error handler
4. Remove dead `persist` references from MCP server code (client.py and server.py)

### Planned Files
- `swarmer/database.py` — Add 5 DROP COLUMN migrations:
  - `ALTER TABLE sessions DROP COLUMN persist`
  - `ALTER TABLE sessions DROP COLUMN privileged`
  - `ALTER TABLE sessions DROP COLUMN pod_name`
  - `ALTER TABLE sessions DROP COLUMN pvc_name`
  - `ALTER TABLE sessions DROP COLUMN k8s_secret_names`
- `swarmer/routers/sessions.py` — In IntegrityError handler:
  - Re-fetch workspace: `ws = await _get_workspace(ws_id, db)`
  - Add template context: `mcp_servers`, `prompt_sources`
- `mcp-server/agent_swarm_mcp_server/client.py` — Remove `persist` parameter from `create_session`
- `mcp-server/agent_swarm_mcp_server/server.py` — Remove `persist` from `_fmt_session`, `create_session`, `update_session` methods and docstrings

### Audit Trail
- Architecture: approve (HIGH) — Simple database migration + error handler fix
- PE: approve (HIGH) — No production impact; migrations are safe
- Language: approve (HIGH) — Idiomatic Python/SQLAlchemy patterns