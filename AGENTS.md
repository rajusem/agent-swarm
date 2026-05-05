# AGENTS.md — Swarmer

A FastAPI + HTMX dashboard for managing AI coding agent workloads on Kubernetes. Supports multiple agent tools (OpenCode, Crush). Server-rendered UI with PatternFly 6 dark theme. Token-based auth via Kubernetes ServiceAccount bearer tokens (+ optional OpenShift OAuth).

## Commands

```sh
# Setup
make setup-secret        # Generate SWARMER_SECRET_KEY → auth/secret.key
make install             # pip install -r requirements.txt

# Development
make dev                 # uvicorn at localhost:8090 with --reload, K8S_IN_CLUSTER=false
make lint                # ruff check swarmer/
make db-reset            # Delete SQLite database (fresh schema on next start)

# Tests
pytest tests/                                        # All tests
pytest tests/test_list_repos_for_pat.py              # Unit tests (no server needed)
pytest tests/test_ui_patternfly.py                   # Playwright UI tests (requires running dev server at :8091 with SWARMER_DEV_AUTH=1)

# Container image
make image-build         # Build container image (podman by default; SILENT=1 to skip version prompt)
make image-push REGISTRY=...  # Push to registry
make image-build-crush   # Build Crush agent container image

# Local kind cluster
make kind-create         # Create kind cluster with NodePort 30080→8080
make kind-load           # Load swarmer image into kind
make kind-load-opencode  # Load opencode agent image into kind
make kind-load-crush     # Load crush agent image into kind
make kind-deploy         # Full one-shot: create cluster + build + load + deploy
make kind-delete         # Tear down kind cluster

# Production Kubernetes
make k8s-deploy          # Deploy to current kubectl context
make k8s-connect         # Port-forward localhost:8080 → swarmer service
make k8s-delete          # Remove all swarmer resources

# User management
make user-token SA_USER=alice                           # Issue a K8s login token (default 8h)
make grant-workspace SA_USER=alice WORKSPACE_NS=my-proj # Grant workspace access
```

## Project Structure

```
agent-swarm/
├── Makefile                    # All build/deploy/dev commands
├── Containerfile               # UBI10 python-312-minimal, runs uvicorn on port 8080
├── Containerfile.crush         # UBI9 minimal + Crush CLI (sleep infinity)
├── requirements.txt            # Pinned minimum versions
├── VERSION                     # Semver used as image tag
├── .env.example                # Copy to .env for local dev
├── scripts/setup_auth.py       # Interactive password setup script (legacy)
├── k8s/                        # Kubernetes manifests
│   ├── kind-config.yaml
│   ├── swarmer/                # Deployment, Service, RBAC, PVC, Namespace
│   └── openshift/              # OpenShift-specific (Route, OAuthClient, Deployment)
├── kustomize/                  # Declarative Kustomize overlays
│   ├── base/common/            # Shared Deployment, PVC, SA
│   ├── base/cluster-admin/     # Full multi-namespace + OAuthClient
│   └── base/namespace-scoped/  # Single-namespace, no cluster-admin
├── plans/                      # Historical implementation plans (read-only reference)
│   └── INDEX.md                # Plan index with Jira/PR links
├── tests/                      # Test suite
│   ├── test_list_repos_for_pat.py   # Unit tests for GitHub API helpers (respx mocking)
│   └── test_ui_patternfly.py        # Playwright e2e tests (requires running server)
└── swarmer/                    # Python package (the application)
    ├── main.py                 # FastAPI app, lifespan, middleware, router registration
    ├── config.py               # pydantic-settings Settings singleton
    ├── database.py             # SQLAlchemy async engine + session factory + migrations
    ├── crypto.py               # Fernet encrypt/decrypt from secret key file or env var
    ├── auth.py                 # (Superseded by k8s_auth.py — single-line comment)
    ├── k8s_auth.py             # K8s TokenReview validation, namespace access check, RBAC probing
    ├── deps.py                 # FastAPI dependencies (require_auth, get_user_token)
    ├── flash.py                # Session-based flash messages
    ├── github.py               # GitHub API helpers (repo info, list repos for PAT)
    ├── ansi.py                 # ANSI SGR → HTML <span> converter (Jinja2 filter)
    ├── k8s.py                  # Kubernetes utility functions (namespace, secret, pod, configmap, route)
    ├── k8s_session.py          # Session-specific K8s ops (PVC, pod spec, service)
    ├── mcp_catalog.py          # Registry of well-known MCP servers (Jira, etc.) with OAuth defaults
    ├── scheduler.py            # Background asyncio cron scheduler for prompt-mode sessions
    ├── log_poller.py           # Background pod log poller with auto-cleanup
    ├── static/
    │   └── htmx.min.js         # Vendored HTMX (no CDN dependency)
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
    └── templates/              # Jinja2 HTML templates (PatternFly 6 dark theme + HTMX)
        ├── base.html           # Layout with masthead, flash messages, PatternFly CDN
        ├── login.html
        ├── auth_callback.html  # OpenShift OAuth implicit flow token capture
        ├── workspaces/         # list, detail, new, edit, _delete_confirm
        ├── sessions/           # list, detail, new, _status_badge, _last_output, _repo_list, crush_chat, etc.
        ├── secrets/            # tabs, opencode_form, github_pat_form, github_pat_list
        └── mcp_servers/        # list (catalog + configured servers with OAuth status)
```

## Architecture & Key Concepts

### Domain Model

- **Workspace** = maps to a Kubernetes namespace. All resources (sessions, secrets) are scoped to a workspace. When `settings.k8s_namespace` is set (namespace-scoped deployment), all workspaces share a single K8s namespace via `Workspace.k8s_namespace` property.
- **Session** = an agent run. Each session creates a K8s Pod + PVC. Three modes:
  - `prompt` — one-shot: runs the agent with a prompt, pod exits on completion (`restartPolicy: Never`), pod + PVC auto-deleted on success if `persist=False`
  - `server` — persistent: runs the agent in server mode, creates a ClusterIP Service (+ OpenShift Route if available), dashboard proxies HTTP/WS/SSE to it
  - `tui` — persistent: runs `sleep infinity`, user connects via xterm.js WebSocket → K8s exec PTY
- **Session phases**: `idle` → `pending` → `running` → `succeeded`/`failed`/`stopped`
- **Cron scheduling** — prompt-mode sessions can have a cron schedule (`cron_schedule` field). A background asyncio loop (`scheduler.py`) checks every 30s, uses an atomic `UPDATE … RETURNING` to claim due rows (prevents duplicates), then calls the shared `_do_launch()` helper in `sessions.py`.
- **OpencodeSecret** — per-workspace encrypted storage for GCP project, Vertex location, ADC JSON, Google API key, Anthropic API key, OpenAI API key. Despite the legacy name, used by both OpenCode and Crush.
- **GitHubPAT** — per-workspace encrypted GitHub personal access tokens with optional org scope for HTTPS git auth
- **McpServer** — per-workspace MCP (Model Context Protocol) server configurations with OAuth 2.1 tokens encrypted at rest. Supports dynamic client registration, PKCE, and token refresh. Enabled servers are injected into agent configs (e.g., Crush's `crush.json` `mcp` section) and their OAuth tokens mounted as K8s secret env vars (`MCP_TOKEN_<SLUG>`). Pre-configured catalog includes Atlassian Jira (Rovo).
- **SessionRepo** — git repositories to clone into the session PVC via init containers

### Agent Tool Strategy Pattern

Multi-agent support uses the Strategy pattern (`agent_tools/`). Each tool (OpenCode, Crush) implements `AgentToolStrategy` with 18+ abstract methods covering:
- Image selection, config map generation, K8s secret layout
- Pod command construction for each session mode
- Model options/validation/selection
- Environment variables, volumes, volume mounts
- Container naming, server ports

The registry (`agent_tools/registry.py`) auto-initializes on import via `_init()`. Tool aliases map legacy names (e.g., `"opencode-golang"` → `"opencode"`).

**To add a new agent tool**: Create `swarmer/agent_tools/new_tool.py` implementing `AgentToolStrategy`, register it in `registry.py:_init()`, add the tool name to `AGENT_TOOLS` in `models/session.py`, and add a corresponding `AGENT_IMAGE_*` setting in `config.py`.

### Authentication

Token-based auth via Kubernetes bearer tokens (not password-based):
- Users paste a K8s ServiceAccount token into the login form
- Token validated via TokenReview API (`k8s_auth.py`); falls back to namespace probe if RBAC for tokenreviews is missing
- Validated token is Fernet-encrypted and stored in the session cookie (`deps.py:get_user_token()`)
- Workspace access controlled by K8s RBAC: `get_accessible_namespaces()` checks which workspace namespaces the token can GET
- Optional OpenShift OAuth: implicit grant flow via `/auth/callback` (captures token from URL fragment client-side)
- `auth.py` is superseded — just contains a comment pointing to `k8s_auth.py`

### Encryption

All sensitive fields (PATs, API keys, ADC credentials) are Fernet-encrypted at rest in SQLite.

- Key source (in priority order): `SWARMER_SECRET_KEY` env var → `auth/secret.key` file → auto-generated on first run
- Key must decode to exactly 32 bytes (base64url-encoded)
- Session cookie secret uses a separate derivation: `SHA256("session:" + raw_key)`
- `crypto.py` must be initialized via `init_crypto()` before any DB access (model property accessors call `decrypt()`)
- Encrypted fields use `_enc` suffix convention (e.g., `pat_enc`, `google_api_key_enc`, `anthropic_api_key_enc`)
- Transparent encrypt/decrypt via Python `@property` getters/setters on models
- Decryption failures (rotated key) return empty string with a warning log, not exceptions

### Database

- **SQLite** via `aiosqlite` + SQLAlchemy 2.x async (`AsyncSession`)
- Database file: `data/swarmer.db` (created automatically on first run)
- Schema created via `Base.metadata.create_all` — no Alembic
- Manual migrations in `database.py:migrate_db()` — uses `ALTER TABLE ... ADD COLUMN` wrapped in try/except (idempotent, only suppresses "duplicate column"/"already exists" errors; other errors re-raise)
- All models must be imported in `models/__init__.py` for table registration to work

### Kubernetes Integration

- Uses the official `kubernetes` Python client, imported lazily inside functions (avoids import errors when K8s isn't configured)
- `k8s.init_k8s()` loads either in-cluster or kubeconfig based on `K8S_IN_CLUSTER` setting
- `effective_namespace()` in `k8s.py` returns `settings.k8s_namespace` if set, otherwise the workspace's own namespace — used in namespace-scoped deployments where all workspaces share one namespace
- Workspace creation → `ensure_namespace()` + `apply_agent_config()` (ConfigMap for each tool)
- Session launch → `ensure_session_pvc()` + `build_session_pod()` + pod creation
- Pod naming: `session-{session_id}-{random_hex_suffix}`
- PVC naming: `session-{session_id}-{suffix}` (shared suffix with pod)
- Init container uses the agent's own image (not alpine/git) to clone configured repos
- OpenShift compatibility: `_grant_anyuid_scc()` creates a RoleBinding for the anyuid SCC; silently skips on non-OpenShift (404/403)
- OpenShift Routes: Created automatically for server-mode sessions; used for direct browser access to the agent's web UI

### Background Tasks

Two background asyncio systems run during app lifespan:

1. **Log Poller** (`log_poller.py`) — Per-session tasks that poll pod status and logs every 5s. Saves phase/detail/output to DB. Auto-cleans up completed prompt-mode pods (deletes pod + PVC if `persist=False`). Restarted for in-flight sessions on app restart via `_restart_prompt_pollers()`.

2. **Cron Scheduler** (`scheduler.py`) — Single global task that checks every 30s for prompt-mode sessions with a due `cron_next_run`. Uses atomic `UPDATE … RETURNING` to claim sessions. On launch failure, resets phase to `idle` and advances `cron_next_run`.

### Chat Proxy

`chat_proxy.py` handles server-mode session access:
- **Crush sessions**: Renders a custom chat UI template (`crush_chat.html`); the JS inside makes API calls through the same `/chat/{path}` proxy
- **OpenCode sessions on OpenShift**: Redirects to the OpenShift Route hostname (direct browser access)
- **OpenCode sessions elsewhere**: Sub-path HTTP proxy with HTML path rewriting (`<base>` tag injection + asset path rewriting)
- SSE streams are proxied with no read timeout
- WebSocket proxy via `websockets` library (bidirectional relay)

### TUI WebSocket Proxy

`tui_ws.py` provides browser-to-pod terminal access:
- One-time UUID auth tokens generated on session detail page, stored in HTTP session, consumed on connect
- Uses `kubernetes.stream` exec API (not kubectl subprocess)
- Background thread reads pod stdout/stderr into an asyncio Queue
- Supports terminal resize via channel 4 JSON messages
- Runs the agent tool's TUI binary (`tool.get_tui_binary()`) with model and resume flags

### Patch Generation

Sessions can generate git diffs from running pods:
- Executes `git diff` (or `git diff origin/{branch}` if using a working branch) via `_exec_in_pod()` 
- AI-generated commit messages via Vertex AI Claude, Anthropic API, or Gemini API (falls back to simple file-list summary)
- Patches downloadable as `.patch` files

### UI Pattern

- **Server-rendered HTML** with Jinja2 templates extending `base.html`
- **PatternFly 6** dark theme via CDN (`pf-v6-theme-dark` on `<html>`)
- **HTMX** for partial page updates (status polling, inline forms, repo management) — vendored as `swarmer/static/htmx.min.js`
- Flash messages stored in Starlette session, rendered in `base.html`
- ANSI escape codes in pod output converted to HTML spans via `ansi_to_html` Jinja2 filter

## Sensitive Data Policy

**NEVER include any of the following in generated code, templates, configs, or comments:**

- API keys, tokens, passwords, or secrets (real or example-looking)
- User IDs, email addresses, or usernames
- GCP project IDs, Vertex locations, or service account details
- Container registry URLs or image references tied to a specific deployment
- Local filesystem paths (e.g. `/home/username/...`, `~/Desktop/...`)
- OAuth client IDs/secrets, kubeconfig contents, or cluster URLs
- Database connection strings with real hostnames or credentials

Use placeholder patterns instead: `<YOUR_PROJECT>`, `example.com`, `your-registry.example.com`, generic variable references (`settings.foo`), or environment variable lookups. Encrypted values must always go through the `crypto.encrypt()`/`crypto.decrypt()` pattern — never store or log plaintext secrets.

## Code Conventions

### Python Style

- Python 3.12, type hints throughout (using `X | None` union syntax, not `Optional`)
- `Mapped[type]` for all SQLAlchemy columns (SQLAlchemy 2.x declarative style)
- Module-level singleton pattern: `settings = Settings()`, `_fernet: Fernet | None = None`
- Lazy kubernetes imports inside functions (avoid import errors when K8s isn't configured)
- `noqa: F401` on model imports in `__init__.py` and forward-reference strings in relationships

### Router Pattern

- Each router creates its own `templates = Jinja2Templates(directory="swarmer/templates")`
- Auth enforced via `dependencies=[Depends(require_auth)]` on every route (except `/login`, `/auth/callback`)
- DB access via `db: AsyncSession = Depends(get_db)`
- POST routes return `RedirectResponse(status_code=302)` (PRG pattern)
- HTMX endpoints return `HTMLResponse` or partial template renders
- Helper functions prefixed with `_` (e.g., `_get_workspace`, `_do_launch`)
- Error handling: `IntegrityError` → rollback + re-render form with error message

### Naming Conventions

- Model files: singular noun (`workspace.py`, `session.py`)
- Router files: plural noun matching the resource (`workspaces.py`, `sessions.py`)
- Template directories: plural noun matching the resource
- HTMX partial templates: prefixed with `_` (e.g., `_status_badge.html`, `_repo_list.html`, `_list_rows.html`)
- K8s resource names: `session-{session_id}-{suffix}` (pods, PVCs), `session-{session_id}-svc` (services), `session-{session_id}-chat` (routes)
- K8s secret names: derived from model fields (e.g., `github-pat-{slug}`, `opencode-secret`, `crush-secret`)
- URL pattern: `/workspaces/{ws_id}/sessions/{sid}/action`

### Configuration

- `pydantic-settings` with `.env` file support, `extra="ignore"` (unrecognized env vars silently ignored)
- All settings have sensible defaults for local development
- Key env vars: `DATABASE_URL`, `SWARMER_SECRET_KEY`, `K8S_IN_CLUSTER`, `K8S_API_URL`, `OPENSHIFT_OAUTH_URL`
- Agent images: `AGENT_IMAGE_OPENCODE`, `AGENT_IMAGE_CRUSH`, `CRUSH_VERSION`, `DEFAULT_AGENT_TOOL`
- Container runtime defaults to `podman` (override with `CONTAINER_CMD=docker`)

### Testing

- Unit tests use `pytest` + `pytest-asyncio` + `respx` for HTTP mocking
- Tests stub model objects with plain classes (`_FakePAT`) to avoid SQLAlchemy/FastAPI dependencies
- Playwright e2e tests require a running dev server with `SWARMER_DEV_AUTH=1` at port 8091
- Test files use `sys.path.insert()` to add the parent dir for imports

## Gotchas & Non-Obvious Patterns

1. **Crypto init order matters**: `init_crypto()` must run before `init_db()` / `create_tables()` because model property accessors call `decrypt()`. The lifespan function in `main.py` enforces this order.

2. **`auth.py` is dead code**: The file `swarmer/auth.py` contains only a comment "superseded by k8s_auth.py". All authentication logic is in `k8s_auth.py` and `routers/auth.py`.

3. **Deployment image placeholder**: `k8s/swarmer/deployment.yaml` uses literal strings like `SWARMER_IMAGE`, `OPENSHIFT_OAUTH_URL_VALUE`, `AGENT_IMAGE_OPENCODE_VALUE`, `AGENT_IMAGE_CRUSH_VALUE` which are replaced at deploy time via `sed` in the Makefile. Don't replace them with actual values.

4. **SQLite single-writer**: The K8s Deployment uses `strategy: Recreate` (not RollingUpdate) because SQLite doesn't support concurrent writers. Only one replica is safe.

5. **Session mode affects pod lifecycle**:
   - `prompt` mode: `restartPolicy: Never`, pod exits after agent finishes, auto-cleaned by log_poller
   - `server`/`tui` modes: `restartPolicy: Always`, pod runs indefinitely
   - Stopping a session always deletes the pod; if `persist=False`, the PVC is also deleted

6. **OpenCode model format quirk**: Model strings use `provider/model@version` format (e.g., `google-vertex-anthropic/claude-sonnet-4-6@default`). The `@version` suffix is part of the model ID. Crush uses simpler `provider/model` format (e.g., `vertexai/claude-sonnet-4-6`).

7. **TUI auth tokens**: TUI WebSocket connections use one-time UUID tokens stored in the HTTP session. Tokens are generated on the session detail page and consumed on WebSocket connect. Invalid/reused tokens are rejected with close code 4001.

8. **Session launch saves working branch**: If no working branch is specified, `session_create` auto-generates one as `swarmer/session-{id}-{hex}` after the initial commit (requires a second commit).

9. **Shared `_do_launch()` function**: Session launch logic is in `routers/sessions.py:_do_launch()` — used by both the HTTP endpoint and the cron scheduler. The scheduler imports it at call time to avoid circular imports.

10. **Manual migrations**: New columns are added via `database.py:migrate_db()` with `ALTER TABLE` statements. Only "duplicate column" / "already exists" errors are suppressed; other failures re-raise so startup fails visibly. When adding a new column to an existing table, add the migration there and include a `server_default` so existing rows work.

11. **Blocking K8s calls in async handlers**: All synchronous `kubernetes` client calls inside async functions must be wrapped with `asyncio.to_thread()` to avoid blocking the event loop. The TUI WebSocket handler uses a background thread with `threading.Event` for the pod exec stream reader.

12. **`OpencodeSecret` naming is misleading**: Despite the name, this model stores credentials for all agent tools (OpenCode, Crush), including Anthropic and OpenAI API keys. The table name `opencode_secrets` is a legacy artifact.

13. **HX-Trigger pattern for repo management**: Repo add/delete endpoints return empty `HTMLResponse` with `HX-Trigger: repoListChanged` header. The template listens for this event to refresh the repo items partial via a separate GET endpoint.

14. **Chat proxy HTML rewriting**: For in-cluster OpenCode server sessions, the proxy injects a `<base>` tag and rewrites absolute asset paths (`src="/..."` → `src="/workspaces/{ws_id}/sessions/{sid}/chat/..."`). Crush sessions skip this and render a custom chat template instead.

15. **`image-build` requires `sync-images`**: The `image-build` Makefile target depends on `sync-images`, which reads `../agent-containers/.push-defaults`. If that file doesn't exist, the build fails. Use `SILENT=1` to skip the interactive version prompt.

16. **Container image runs as non-root**: The Containerfile uses UBI10 `python-312-minimal` with UID 1001. Directories `/data` and `/auth` are created as root then ownership dropped. PVCs must be group-0 writable for the non-root user.

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
