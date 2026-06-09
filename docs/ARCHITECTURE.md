# Swarmer — Architecture Reference

## Project Structure

```
agent-swarm/
├── Makefile                    # All build/deploy/dev commands
├── Containerfile               # UBI10 python-312-minimal, runs uvicorn on port 8080
├── Containerfile.crush         # UBI9 minimal + Crush CLI (sleep infinity)
├── requirements.txt            # Pinned minimum versions
├── VERSION                     # Semver used as image tag
├── .env.example                # Copy to .env for local dev
├── k8s/                        # Kubernetes manifests
│   ├── kind-config.yaml
│   ├── swarmer/                # Deployment, Service, RBAC, PVC, Namespace
│   └── openshift/              # OpenShift-specific (Route, OAuthClient, Deployment)
├── kustomize/                  # Declarative Kustomize overlays
│   ├── base/common/            # Shared Deployment, PVC, SA
│   ├── base/cluster-admin/     # Full multi-namespace + OAuthClient
│   └── base/namespace-scoped/  # Single-namespace, no cluster-admin
├── docs/                       # Documentation
│   ├── USER_GUIDE.md           # Full user-facing guide
│   └── ARCHITECTURE.md         # This file
├── mcp-server/                 # Standalone MCP server for session orchestration
├── tests/                      # Test suite
│   ├── test_api.py              # REST API unit tests (in-memory SQLite, no server)
│   ├── test_list_repos_for_pat.py  # GitHub API helpers (respx mocking)
│   ├── test_openshell_client.py # OpenShell client wrapper tests (mocked SDK, no package required)
│   └── test_ui_patternfly.py   # Playwright e2e tests (requires running server at :8091)
└── swarmer/                    # Python package (the application)
    ├── main.py                 # FastAPI app, lifespan, middleware, router registration
    ├── config.py               # pydantic-settings Settings singleton
    ├── database.py             # SQLAlchemy async engine + session factory + migrations
    ├── crypto.py               # Fernet encrypt/decrypt from secret key file or env var
    ├── k8s_auth.py             # K8s TokenReview validation, namespace access check, RBAC probing
    ├── deps.py                 # FastAPI dependencies (require_auth, get_user_token)
    ├── k8s.py                  # Kubernetes utility functions (namespace, secret, pod, configmap, route)
    ├── k8s_session.py          # Session-specific K8s ops (PVC, pod spec, service)
    ├── mcp_catalog.py          # Registry of well-known MCP servers (Jira, etc.) with OAuth defaults
    ├── scheduler.py            # Background asyncio cron scheduler for prompt-mode sessions
    ├── log_poller.py           # Background pod log poller with auto-cleanup
    ├── openshell_client.py     # OpenShell sandbox SDK wrapper (async helpers, lazy SDK import)
    ├── agent_tools/            # Strategy pattern for multi-agent support
    │   ├── __init__.py         # AgentToolStrategy ABC (18 abstract methods)
    │   ├── registry.py         # Global registry + aliases (_init() auto-registers all tools)
    │   ├── opencode.py         # OpenCode strategy (Vertex AI Anthropic/Gemini models)
    │   └── crush.py            # Crush strategy (Vertex AI, Anthropic, OpenAI, Gemini models)
    ├── models/                 # SQLAlchemy ORM models
    │   ├── __init__.py         # Imports all models (required for Base.metadata)
    │   ├── workspace.py        # Workspace → 1:1 K8s namespace (or shared via settings.k8s_namespace)
    │   ├── session.py          # Session (pod lifecycle, modes: tui/server/prompt, cron scheduling)
    │   ├── session_repo.py     # Git repos attached to sessions (cloned by init container)
    │   ├── opencode_secret.py  # Fernet-encrypted provider credentials (GCP/Anthropic/OpenAI/Gemini)
    │   ├── github_pat.py       # Fernet-encrypted GitHub PATs for HTTPS git auth
    │   └── mcp_server.py       # MCP server configs with Fernet-encrypted OAuth tokens
    ├── routers/                # FastAPI route handlers
    │   ├── auth.py             # /login (token paste + OpenShift OAuth), /logout, /auth/callback
    │   ├── workspaces.py       # CRUD for workspaces
    │   ├── sessions.py         # CRUD + launch/stop/schedule/patch generation + repo management
    │   ├── secrets.py          # OpenCode secrets, GitHub PATs, pull secrets
    │   ├── mcp_servers.py      # MCP server CRUD, OAuth 2.1 flow (PKCE + dynamic registration)
    │   ├── chat_proxy.py       # HTTP/SSE/WebSocket reverse proxy for server-mode sessions
    │   └── tui_ws.py           # WebSocket PTY proxy for TUI-mode sessions (K8s exec)
    ├── api/v1/                 # REST API — 51 endpoints under /api/v1/
    └── templates/              # Jinja2 HTML templates (PatternFly 6 dark theme + HTMX)
        ├── base.html           # Layout with masthead, flash messages, PatternFly CDN
        ├── workspaces/         # list, detail, new, edit, _delete_confirm
        ├── sessions/           # list, detail, new, _status_badge, _last_output, _repo_list, crush_chat, etc.
        ├── secrets/            # tabs, opencode_form, github_pat_form, github_pat_list
        └── mcp_servers/        # list (catalog + configured servers with OAuth status)
```

## Domain Model

- **Workspace** maps to a Kubernetes namespace. All resources (sessions, secrets) are scoped to a workspace. When `settings.k8s_namespace` is set (namespace-scoped deployment), all workspaces share a single K8s namespace via `Workspace.k8s_namespace` property.
- **Session** = an agent run. Each session creates a K8s Pod + PVC. Three modes:
  - `prompt` — one-shot: runs the agent with a prompt, pod exits on completion (`restartPolicy: Never`), pod + PVC auto-deleted on success if `persist=False`
  - `server` — persistent: runs the agent in server mode, creates a ClusterIP Service (+ OpenShift Route if available), dashboard proxies HTTP/WS/SSE to it
  - `tui` — persistent: runs `sleep infinity`, user connects via xterm.js WebSocket → K8s exec PTY
- **Session phases**: `idle` → `pending` → `running` → `succeeded`/`failed`/`stopped`
- **Cron scheduling** — prompt-mode sessions can have a cron schedule (`cron_schedule` field). A background asyncio loop (`scheduler.py`) checks every 30s, uses an atomic `UPDATE … RETURNING` to claim due rows (prevents duplicates), then calls the shared `_do_launch()` helper in `sessions.py`.
- **OpencodeSecret** — per-workspace encrypted storage for GCP project, Vertex location, ADC JSON, Google API key, Anthropic API key, OpenAI API key. Despite the legacy name, used by both OpenCode and Crush.
- **GitHubPAT** — per-workspace encrypted GitHub personal access tokens with optional org scope for HTTPS git auth
- **McpServer** — per-workspace MCP server configurations with OAuth 2.1 tokens encrypted at rest. Supports dynamic client registration, PKCE, and token refresh. Enabled servers are injected into agent configs and mounted as K8s secret env vars (`MCP_TOKEN_<SLUG>`). Pre-configured catalog includes Atlassian Jira (Rovo).
- **SessionRepo** — git repositories to clone into the session PVC via init containers

## Agent Tool Strategy Pattern

Multi-agent support uses the Strategy pattern (`agent_tools/`). Each tool (OpenCode, Crush) implements `AgentToolStrategy` with 18+ abstract methods covering:
- Image selection, config map generation, K8s secret layout
- Pod command construction for each session mode
- Model options/validation/selection
- Environment variables, volumes, volume mounts
- Container naming, server ports

The registry (`agent_tools/registry.py`) auto-initializes on import via `_init()`. Tool aliases map legacy names (e.g., `"opencode-golang"` → `"opencode"`).

**To add a new agent tool**: Create `swarmer/agent_tools/new_tool.py` implementing `AgentToolStrategy`, register it in `registry.py:_init()`, add the tool name to `AGENT_TOOLS` in `models/session.py`, and add a corresponding `AGENT_IMAGE_*` setting in `config.py`.

## Authentication

Token-based auth via Kubernetes bearer tokens (not password-based):
- Users paste a K8s ServiceAccount token into the login form
- Token validated via TokenReview API (`k8s_auth.py`); falls back to namespace probe if RBAC for tokenreviews is missing
- Validated token is Fernet-encrypted and stored in the session cookie (`deps.py:get_user_token()`)
- Workspace access controlled by K8s RBAC: `get_accessible_namespaces()` checks which workspace namespaces the token can GET
- Optional OpenShift OAuth: implicit grant flow via `/auth/callback` (captures token from URL fragment client-side)
- `swarmer/auth.py` is superseded — just contains a comment pointing to `k8s_auth.py`

## Encryption

All sensitive fields (PATs, API keys, ADC credentials) are Fernet-encrypted at rest in SQLite.

- Key source (in priority order): `SWARMER_SECRET_KEY` env var → `auth/secret.key` file → auto-generated on first run
- Key must decode to exactly 32 bytes (base64url-encoded)
- Session cookie secret uses a separate derivation: `SHA256("session:" + raw_key)`
- `crypto.py` must be initialized via `init_crypto()` before any DB access (model property accessors call `decrypt()`)
- Encrypted fields use `_enc` suffix convention (e.g., `pat_enc`, `google_api_key_enc`)
- Transparent encrypt/decrypt via Python `@property` getters/setters on models
- Decryption failures (rotated key) return empty string with a warning log, not exceptions

## Database

- **SQLite** via `aiosqlite` + SQLAlchemy 2.x async (`AsyncSession`)
- Database file: `data/swarmer.db` (created automatically on first run)
- Schema created via `Base.metadata.create_all` — no Alembic
- Manual migrations in `database.py:migrate_db()` — uses `ALTER TABLE ... ADD COLUMN` wrapped in try/except (idempotent; only suppresses "duplicate column"/"already exists" errors, all others re-raise)
- All models must be imported in `models/__init__.py` for table registration to work
- SQLite single-writer: K8s Deployment uses `strategy: Recreate` (not RollingUpdate)

## Kubernetes Integration

- Uses the official `kubernetes` Python client, imported lazily inside functions (avoids import errors when K8s isn't configured)
- `k8s.init_k8s()` loads either in-cluster or kubeconfig based on `K8S_IN_CLUSTER` setting
- `effective_namespace()` in `k8s.py` returns `settings.k8s_namespace` if set, otherwise the workspace's own namespace — used in namespace-scoped deployments where all workspaces share one namespace
- Workspace creation → `ensure_namespace()` + `apply_agent_config()` (ConfigMap for each tool)
- Session launch → `ensure_session_pvc()` + `build_session_pod()` + pod creation
- Pod naming: `session-{session_id}-{random_hex_suffix}`; PVC naming: `session-{session_id}-{suffix}`
- Init container uses the agent's own image (not alpine/git) to clone configured repos
- OpenShift compatibility: `_grant_anyuid_scc()` creates a RoleBinding for the anyuid SCC; silently skips on non-OpenShift (404/403)
- OpenShift Routes: created automatically for server-mode sessions

## OpenShell Integration

[NVIDIA OpenShell](https://github.com/nvidia/openshift-ai-openShell) replaces direct K8s pod and Secret management with a Gateway + Supervisor model. Swarmer sends credentials to the Gateway (which injects them securely) and requests sandboxes from the Supervisor (which provides the isolated runtime). Swarmer never writes AI tokens or PATs into K8s Secrets again.

- **Gateway** -- credential injection API; Swarmer sends AI tokens, PATs, and MCP tokens to the Gateway instead of writing K8s Secrets via `envFrom`
- **Supervisor** -- sandboxed agent runtime; replaces `build_session_pod()` + `create_namespaced_pod()`
- **Sandbox lifecycle** -- `create_sandbox()` replaces `build_session_pod()` + `create_namespaced_pod()`; `delete_sandbox()` replaces `delete_pod()` + PVC cleanup
- **No PVCs** -- sandbox lifetime is managed by OpenShell; `session.persist` and PVC lifecycle are removed in the final cleanup sub-task
- **No K8s Secrets** -- credentials are injected via Gateway env vars, not `envFrom` K8s Secrets; `session.k8s_secret_names` becomes unused
- **`session.sandbox_name`** -- stores the OpenShell sandbox identifier; analogous to `session.pod_name` for the K8s path (nullable `VARCHAR(255)`, `NULL` when K8s path is active)
- **Network policy** -- `openshell_policy.py` builds per-sandbox YAML policies controlling outbound access (AI provider endpoints, per-repo GitHub, Jira MCP)
- **Client module** -- `swarmer/openshell_client.py` wraps the OpenShell gRPC SDK with async helpers using `asyncio.to_thread`

### OpenShell Client API (`swarmer/openshell_client.py`)

| Function | Signature | Description |
|---|---|---|
| `_get_client()` | `() → SandboxClient` | Internal factory; reads settings, returns configured SDK client |
| `get_client()` | `(gateway_url, tls_ca_path?, tls_cert_path?, tls_key_path?) → SandboxClient` | Public factory for e2e tests |
| `create_provider()` | `async (session, workspace_secret, github_pat, mcp_servers, client?) → dict[str,str]` | Collects DB credentials into env-var dict (no K8s Secrets, no I/O) |
| `create_provider_from_env()` | `async (google_api_key, anthropic_api_key, github_pat, client?) → dict[str,str]` | Builds env-var dict from explicit values (for tests) |
| `ensure_provider()` | `async (name, profile_type, config, credentials?, client?) → None` | Creates or updates a named gateway provider (idempotent) |
| `configure_provider_credential()` | `async (provider_name, credential_key, credential_value, client?) → None` | Stores a static credential on a gateway-managed provider |
| `configure_vertex_provider()` | `async (provider_name, adc_json, project, location, client?) → None` | Configures google-vertex-ai provider with ADC-based token refresh |
| `enable_providers_v2()` | `async (client?) → None` | Enables `providers_v2_enabled` gateway feature flag (required for google-vertex-ai) |
| `set_cluster_inference()` | `async (provider_name, model_id, no_verify?, client?) → None` | Configures inference.local cluster proxy to use a provider+model |
| `create_sandbox()` | `async (image, env_vars, policy, provider_names?, client?) → SandboxRef` | Creates sandbox, waits ready, returns ref |
| `delete_sandbox()` | `async (sandbox_name, client?) → None` | Deletes sandbox by name |
| `write_agent_config()` | `async (sandbox_name, tool_name, config_json, client?) → None` | Writes tool config JSON to `/sandbox/{tool}.json` |
| `write_agents_md()` | `async (sandbox_name, content, client?) → None` | Writes AGENTS.md to `/sandbox/` |
| `write_file()` | `async (sandbox_name, path, content, client?) → None` | Writes arbitrary file to sandbox |
| `start_agent()` | `async (sandbox_name, cmd, client?) → None` | Starts agent as detached nohup background process (fire-and-forget) |
| `exec_command()` | `async (sandbox_name, cmd, client, stdin?, timeout_seconds?) → ExecResult` | Runs command, returns result with stdout/stderr/exit_code |
| `exec_interactive()` | `(sandbox_name, sandbox_id, command, cols, rows, client?) → (stream, queue)` | Opens interactive PTY gRPC stream for TUI WebSocket bridge |
| `expose_service()` | `async (sandbox_name, service_name, target_port, client?) → str` | Exposes sandbox port via gateway and returns a routable URL |
| `delete_service()` | `async (sandbox_name, service_name, client?) → None` | Deletes an exposed sandbox service endpoint |
| `approve_draft_policy_chunks()` | `async (sandbox_name, expected_hosts?, client?) → list[str]` | Approves pending network policy chunks for expected hosts |

### OpenShell Config Settings

All settings live in `swarmer/config.py` (`Settings` class) and are read from env vars:

| Setting | Env Var | Type | Default | Purpose |
|---|---|---|---|---|
| `openshell_gateway_url` | `OPENSHELL_GATEWAY_URL` | `str` | `""` | Gateway API base URL for credential injection |
| `openshell_supervisor_url` | `OPENSHELL_SUPERVISOR_URL` | `str` | `""` | Supervisor API base URL for sandbox lifecycle |
| `openshell_tls_cert` | `OPENSHELL_TLS_CERT` | `str` | `""` | Path to client TLS certificate (mTLS) |
| `openshell_tls_key` | `OPENSHELL_TLS_KEY` | `str` | `""` | Path to client TLS private key (mTLS) |
| `openshell_tls_ca` | `OPENSHELL_TLS_CA` | `str` | `""` | Path to CA bundle for server cert verification |
| `openshell_bearer_token` | `OPENSHELL_BEARER_TOKEN` | `str` | `""` | Bearer token for Gateway/Supervisor authentication |
| `sandbox_gc_interval` | `SANDBOX_GC_INTERVAL` | `int` | `300` | Seconds between sandbox garbage-collection sweeps |

### OpenShell Config Settings

All settings live in `swarmer/config.py` (`Settings` class) and are read from env vars:

| Setting | Env Var | Type | Default | Purpose |
|---|---|---|---|---|
| `openshell_enabled` | `OPENSHELL_ENABLED` | `bool` | `False` | Feature flag — enables OpenShell path; K8s is the fallback |
| `openshell_gateway_url` | `OPENSHELL_GATEWAY_URL` | `str` | `""` | Gateway API base URL for credential injection |
| `openshell_supervisor_url` | `OPENSHELL_SUPERVISOR_URL` | `str` | `""` | Supervisor API base URL for sandbox lifecycle |
| `openshell_tls_cert` | `OPENSHELL_TLS_CERT` | `str` | `""` | Path to client TLS certificate (mTLS) |
| `openshell_tls_key` | `OPENSHELL_TLS_KEY` | `str` | `""` | Path to client TLS private key (mTLS) |
| `openshell_tls_ca` | `OPENSHELL_TLS_CA` | `str` | `""` | Path to CA bundle for server cert verification |
| `openshell_bearer_token` | `OPENSHELL_BEARER_TOKEN` | `str` | `""` | Bearer token for Gateway/Supervisor authentication |
| `sandbox_gc_interval` | `SANDBOX_GC_INTERVAL` | `int` | `300` | Seconds between sandbox garbage-collection sweeps |

## Agent Container Data Interface

Every data item Swarmer currently pushes into agent pods, its source model, the current K8s mechanism, and the target OpenShell API call. This table is the migration contract for ACM-34850.

| Category | Data | Source Model | Current K8s Mechanism | Target OpenShell API |
|---|---|---|---|---|
| AI Credentials | GCP Project, Vertex Location, ADC JSON, Gemini key, Anthropic key, OpenAI key | `OpencodeSecret` | K8s Secret → `envFrom` | Gateway `create_provider()` env injection |
| Git Auth | PAT token, GitHub username | `GitHubPAT` | K8s Secret → `secretKeyRef` + init container credential store | Gateway credential injection + `clone_repos()` |
| Git Repos | repo_url, branch, local_path (per repo) | `SessionRepo` | Init container git clone | `openshell_client.clone_repos()` |
| MCP Tokens | Jira URL, Jira access token, Jira email | `McpServer` | K8s Secret → `envFrom` | Gateway env injection |
| Agent Config | Tool-specific JSON + gitconfig | ConfigMap | Volume mount at `/tmp/agent-config-ro` | `write_agent_config()` into sandbox |
| MCP Config | MCP server definitions in agent config JSON | `McpServer` | Startup script overwrites config | `write_file()` into sandbox |
| Model Config | model.json (OpenCode) or crush.json (Crush) | `Session.model` | Startup script writes JSON file | `write_file()` into sandbox |
| Prompt | instruction_prompt + base_prompt + repo_context | `Session` + `WorkspacePrompt` | CLI arg (prompt mode) or `SWARMER_AGENT_MD` env → AGENTS.md (TUI/server) | `write_file()` for AGENTS.md; CLI arg for prompt mode |
| Env Vars | HOME, NODE_OPTIONS, GOOGLE_APPLICATION_CREDENTIALS | Hardcoded | Pod env spec | Sandbox env vars via Gateway |
| Extra Env | Arbitrary workspace key-value pairs | External K8s Secret | `envFrom` (`swarmer-agent-extra-env`, optional) | Gateway env injection |
| Volumes | PVC → /workspace, ConfigMap → /tmp/agent-config-ro, ADC → /app/gcloud | N/A | Pod volume spec | Sandbox filesystem (no separate volumes) |
| Startup Script | Config copy, safe dir, git creds, symlinks, AGENTS.md write, model write, branch checkout | N/A | `sh -c` command chain | Simplified script — removes credential setup and git clone stages |
| Pod Config | Resources (1Gi-8Gi/500m-2000m), fsGroup, runAsUser, imagePullPolicy, restartPolicy | `Session` + `Settings` | Pod spec | Sandbox resource config |
| Networking | Container port 4096 (server mode), ClusterIP Service, OpenShift Route | `Session.mode` | K8s Service/Route | OpenShell network endpoint |

- **Startup script simplification** -- the OpenShell startup script removes: credential helper setup, git clone, `envFrom` secret injection, ADC volume mount. Keeps: config copy, MCP config overwrite, model JSON write, AGENTS.md write, branch checkout, agent binary invocation
- **Filesystem layout** -- `/sandbox` replaces `/workspace` as the agent HOME and git clone root in OpenShell mode; `stolostron/agent-containers` images must support this path
- **Init containers removed** -- repo cloning moves to `openshell_client.clone_repos()`; the `git-init` init container is eliminated

## Background Tasks

Two background asyncio systems run during app lifespan:

1. **Log Poller** (`log_poller.py`) — Per-session tasks that poll pod status and logs every 5s. Saves phase/detail/output to DB. Auto-cleans up completed prompt-mode pods (deletes pod + PVC if `persist=False`). Restarted for in-flight sessions on app restart via `_restart_prompt_pollers()`.

2. **Cron Scheduler + Queue Processor** (`scheduler.py`) — Single global task that checks every 30s. Each cycle:
   - **Queue processor** (`_process_queue`): If the global concurrency cap is not reached, fetches sessions in `"queued"` phase ordered by `created_at` (FIFO) and launches them up to the available slot count. Applies a 2-minute in-memory cooldown when still at capacity to avoid tight retry loops.
   - **Cron launcher**: Claims prompt-mode sessions with a due `cron_next_run` via atomic `UPDATE … RETURNING`. Respects the concurrency cap — does not over-claim. On launch failure, resets phase to `idle` and advances `cron_next_run`.

### Concurrency Limiting

`MAX_CONCURRENT_AGENTS` (default 5, configurable via env var) caps the number of simultaneously running agent pods (sessions in `pending` or `running` phase). When this limit is reached:

- All new launches (manual, API, or scheduled) set `phase="queued"` and return immediately without creating a pod.
- The queue processor re-evaluates every 2 minutes and launches queued sessions as capacity frees up.
- Stopping a queued session (no pod exists) returns it directly to `"idle"` without any K8s cleanup.
- Setting `MAX_CONCURRENT_AGENTS=0` disables the limit entirely.

The sessions list shows a workspace-scoped capacity summary ("N active | N slots available | N queued") that refreshes every 3s via HTMX. Queued sessions show their global queue position ("Position N of M") on both the list and detail pages.

## Chat Proxy

`chat_proxy.py` handles server-mode session access. The proxy backend is selected by `session.sandbox_name`:

- **OpenShell sessions** (`session.sandbox_name` set): routes HTTP/SSE/WebSocket to `session.service_url` — set by `expose_service()` after the server agent starts. The URL is an OpenShell gateway domain URL (e.g. `https://<name>.openshell.localhost:<port>`). The proxy rewrites the port in `expose_service` to match `OPENSHELL_GATEWAY_URL` (the locally accessible port-forward port). The gateway requires **mutual TLS** — the proxy presents the client cert/key from `OPENSHELL_TLS_CERT`/`OPENSHELL_TLS_KEY` and skips server cert verification (`verify=False`) since the gateway uses a self-signed cert. Without the client cert the gateway returns `TLSV13_ALERT_CERTIFICATE_REQUIRED`.
- **K8s sessions** (`session.pod_name` set, no `sandbox_name`): routes to the session's ClusterIP Service via `http://session-{id}-svc.{namespace}.svc.cluster.local:4096`
- **OpenShift Route sessions**: redirects browser directly to the OpenShift Route hostname (bypasses proxy)
- **Crush sessions**: renders a custom `crush_chat.html` template; JS calls go through the same `/chat/{path}` proxy

Server-mode lifecycle: session stays in `pending` until `expose_service` returns a URL, which is stored and the session transitions to `running` atomically — preventing the Chat tab from opening before the URL is set.

SSE streams proxied with no read timeout; WebSocket proxy via `websockets` library (bidirectional relay) with TLS bypass for `wss://` upstreams.

## TUI WebSocket Proxy

`tui_ws.py` provides browser-to-agent terminal access. The backend is selected by `session.sandbox_name`:

**OpenShell path** (`session.sandbox_name` set):
- Resolves `sandbox_id` via `_sandbox_id()`
- Opens an `ExecSandboxInteractive` gRPC stream via `exec_interactive()`
- Background thread drains the gRPC response stream into an asyncio Queue
- Async read/write tasks bridge the browser xterm.js WebSocket and the gRPC stream
- Resize events forwarded as `ExecSandboxWindowResize` messages
- Agent is NOT started here — the TUI WebSocket handler starts it interactively; `_run_openshell_agent` skips `start_agent` for TUI mode
- Network policy probe runs during `_setup_openshell_sandbox` so AI API endpoints are approved before the user connects

**K8s path** (`session.pod_name` set, no `sandbox_name`):
- One-time UUID auth tokens generated on session detail page, stored in HTTP session, consumed on connect
- Uses `kubernetes.stream` exec API (not kubectl subprocess)
- Background thread reads pod stdout/stderr into an asyncio Queue
- Supports terminal resize via channel 4 JSON messages

Both paths run the agent tool's TUI binary (`tool.get_tui_binary()`) with model and resume flags.

## Patch Generation

Sessions can generate git diffs from running pods:
- Executes `git diff` (or `git diff origin/{branch}` if using a working branch) via `_exec_in_pod()`
- AI-generated commit messages via Vertex AI Claude, Anthropic API, or Gemini API (falls back to simple file-list summary)
- Patches downloadable as `.patch` files

## UI Pattern

- **Server-rendered HTML** with Jinja2 templates extending `base.html`
- **PatternFly 6** dark theme via CDN (`pf-v6-theme-dark` on `<html>`)
- **HTMX** for partial page updates (status polling, inline forms, repo management) — vendored as `swarmer/static/htmx.min.js`
- Flash messages stored in Starlette session, rendered in `base.html`
- ANSI escape codes in pod output converted to HTML spans via `ansi_to_html` Jinja2 filter

## Adding New Features

### Adding a new model field

1. Add the column to the SQLAlchemy model in `swarmer/models/`
2. If the table already exists in production DBs, add an `ALTER TABLE` migration in `database.py:migrate_db()`
3. Include `server_default=` so existing rows get a valid value

### Adding a new router

1. Create `swarmer/routers/new_feature.py` with `router = APIRouter()`
2. Add `dependencies=[Depends(require_auth)]` to all routes
3. Import and register in `swarmer/main.py`: `app.include_router(new_router.router)`

### Adding a new model

1. Create `swarmer/models/new_model.py` inheriting from `Base`
2. Import it in `swarmer/models/__init__.py` (required for table creation)
3. If it has encrypted fields, follow the `_enc` suffix + `@property` pattern from `github_pat.py`

### Adding secrets/sensitive fields

1. Store the encrypted value with `_enc` suffix
2. Add `@property` getter calling `crypto.decrypt()` and `@setter` calling `crypto.encrypt()`
3. Sync to K8s Secret via the agent tool's `build_k8s_secret_data()` method

### Adding a new agent tool

1. Create `swarmer/agent_tools/new_tool.py` implementing all `AgentToolStrategy` abstract methods
2. Register in `agent_tools/registry.py:_init()`
3. Add the tool name to `AGENT_TOOLS` tuple in `models/session.py`
4. Add `agent_image_new_tool: str = ""` in `config.py:Settings`
5. Add corresponding `AGENT_IMAGE_NEWTOOL` env var in `.env.example` and Makefile placeholders

### Adding a new MCP server (OpenShell sandbox network policy)

OpenShell sandboxes enforce outbound network access at **two layers** — both must be configured
or the MCP server will fail to connect even if one layer is open:

1. **OPA/Landlock** — controls which binary processes may open which network connections.
   Configured via `swarmer/openshell_policy.py`. Each rule is a `{host, port, binary}` triplet.
2. **Egress proxy** (`HTTP_PROXY=10.200.0.1:3128`) — a CONNECT proxy that gates all sandbox
   HTTPS traffic. It enforces the same OPA policy at the proxy layer. A wildcard like
   `*.example.com` in the OPA rule may not be sufficient; the proxy may require a literal
   host match as well (confirmed with `redhat.atlassian.net` — wildcard `*.atlassian.net`
   alone produced a 403 at the proxy; adding the literal host fixed it).

**The key gotcha — OPA resolves canonical binary paths:**

OPA identifies processes by resolving symlinks via `/proc/{pid}/root`. It sees the canonical
binary path, not the symlink. For example, `/usr/bin/python3 → python3.14`, so the rule
must list `/usr/local/bin/python3.14` (confirmed via OPA draft chunks), not just
`/usr/bin/python3`. Always check draft chunks after the first run to discover the actual
path OPA reports.

**Step-by-step: adding a new MCP server**

1. **Add the catalog entry** in `swarmer/mcp_catalog.py` with `slug`, `display_name`,
   `command`, and any credential defaults.

2. **Add credential fields** to `McpServer` in `swarmer/models/mcp_server.py` following the
   `_enc` suffix + `@property` encrypt/decrypt pattern. Add an `ALTER TABLE` migration in
   `database.py:migrate_db()`.

3. **Inject credentials into the sandbox** in `swarmer/openshell_client.py:create_provider()`.
   Match on `"<slug-keyword>" in getattr(mcp, "slug", "")` (loose match) and populate
   `env_vars` from the model's direct fields. Credentials go into `SandboxSpec.environment`
   at sandbox creation time. Note: `spec.environment` reaches the supervisor-launched agent
   process but **not** ad-hoc `client.exec()` calls — write a `/sandbox/.tool.env` file via
   `stdin` if exec commands also need the vars.

4. **Add the network policy block** in `swarmer/openshell_policy.py`:
   - Add a `_TOOL_MCP_BLOCK` constant with `endpoints` and `binaries`.
   - For `binaries`: list both the entry-point binary (`/usr/local/bin/tool-server`) and
     the underlying interpreter if it is a scripted tool (e.g. `python3.14`, `node`).
     Use `_bin(path)` for every entry — `harness=True` is mandatory.
   - For `endpoints`: list both a wildcard (`*.example.com`) **and** the specific literal
     hostname (`tenant.example.com`) used in production. Wildcards alone are unreliable at
     the proxy layer.
   - Wire the block into `build_session_network_policies()` with a slug keyword check:
     `if any("<keyword>" in getattr(mcp, "slug", "") for mcp in (mcp_servers or []))`.

5. **Update the unit tests** in `tests/test_openshell_policy.py` and
   `tests/test_openshell_client.py`. Use the real `slug` value from the catalog (not a
   synthetic `"jira"` shorthand) so the tests catch slug-mismatch bugs.

6. **Write a dedicated e2e smoke test** at `scripts/openshell_<tool>_smoke_test.py`.
   Use `scripts/openshell_jira_smoke_test.py` as the template. The test must:
   - Read credentials from the **process environment** (never from Python variables) —
     source the tool's `.env` file before running: `set -a && source .env && set +a`.
   - Write credentials into the sandbox via `stdin` to `/sandbox/.tool.env`, then
     `source` that file in subsequent `exec()` calls.
   - Validate network access with `curl -v` using env var refs (`$VAR`) inside the sandbox
     shell — the `-v` output reveals whether the failure is a proxy 403 (policy gap) or a
     DNS/TLS error (different problem).
   - Run the MCP server binary with a JSON-RPC `initialize` request over stdin. A valid
     MCP `initialize` response confirms the full stack works end-to-end.
   - After the run, query `GetDraftPolicy` on the sandbox to surface any pending OPA draft
     chunks — these are the **policy sub-bumps** (missing binary or host entries) that need
     to be added to the policy block before the tool will work reliably.

   **Iterating on policy gaps (the sub-bump loop):**

   ```
   Run smoke test
     → step 9 fails with ProxyError / ConnectionError
     → query GetDraftPolicy on the sandbox (before cleanup)
     → draft chunk shows: binary=/usr/local/bin/python3.14 host=tenant.example.com
     → add that binary + literal host to the policy block constant
     → re-run smoke test
     → repeat until 18/18 (or N/N) passes with "No OPA network denials"
   ```

   To inspect draft chunks mid-test without cleanup, temporarily add this after the
   jira-mcp-server exec step:
   ```python
   req = openshell_pb2.GetDraftPolicyRequest()
   req.name = sandbox_name
   resp = client._stub.GetDraftPolicy(req, timeout=10)
   for c in resp.chunks:
       print(c.proposed_rule.binaries[0].path, c.proposed_rule.endpoints[0].host)
   ```

7. **Run the smoke test against a real cluster** (OpenShell gateway must be reachable via
   port-forward or direct URL):
   ```sh
   set -a && source ../my-mcp-server/.env && set +a
   python3 scripts/openshell_<tool>_smoke_test.py
   ```

**Reference implementation:** `scripts/openshell_jira_smoke_test.py` + `_JIRA_MCP_BLOCK`
in `swarmer/openshell_policy.py` — worked through the full sub-bump loop to reach 18/18.
