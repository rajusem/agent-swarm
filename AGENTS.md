# AGENTS.md — Swarmer

A FastAPI + HTMX dashboard for managing [opencode](https://opencode.ai) agent workloads on Kubernetes. Single-user, password-protected web UI that creates/manages Kubernetes namespaces, secrets, and session pods.

## Commands

```sh
# Setup
make setup-auth          # Set dashboard password (writes auth/password.hash)
make install             # pip install -r requirements.txt

# Development
make dev                 # uvicorn at localhost:8090 with --reload, K8S_IN_CLUSTER=false
make lint                # ruff check swarmer/
make db-reset            # Delete SQLite database (fresh schema on next start)

# Container image
make image-build         # Build container image (podman by default, set CONTAINER_CMD=docker)
make image-push REGISTRY=...  # Push to registry

# Local kind cluster
make kind-create         # Create kind cluster with NodePort 30080→8080
make kind-load           # Load swarmer image into kind
make kind-load-opencode  # Load opencode-golang agent image into kind
make kind-deploy         # Full one-shot: create cluster + build + load + deploy
make kind-delete         # Tear down kind cluster

# Production Kubernetes
make k8s-deploy          # Deploy to current kubectl context
make k8s-connect         # Port-forward localhost:8080 → swarmer service
make k8s-delete          # Remove all swarmer resources
```

**No test suite exists.** There are no test files, no `tests/` directory, and no test commands in the Makefile.

## Project Structure

```
agent-swarm/
├── Makefile                    # All build/deploy/dev commands
├── Containerfile               # Python 3.12-slim, runs uvicorn on port 8080
├── requirements.txt            # Pinned minimum versions
├── .env.example                # Copy to .env for local dev
├── scripts/setup_auth.py       # Interactive password setup script
├── k8s/                        # Kubernetes manifests
│   ├── kind-config.yaml
│   └── swarmer/                # Deployment, Service, RBAC, PVC, Namespace
├── plans/                      # Historical implementation plans (read-only reference)
└── swarmer/                    # Python package (the application)
    ├── main.py                 # FastAPI app, lifespan, middleware, router registration
    ├── config.py               # pydantic-settings Settings singleton
    ├── database.py             # SQLAlchemy async engine + session factory + migrations
    ├── crypto.py               # Fernet encrypt/decrypt derived from auth hash file
    ├── auth.py                 # Argon2 password verification
    ├── deps.py                 # FastAPI dependencies (require_auth)
    ├── flash.py                # Session-based flash messages
    ├── k8s.py                  # Kubernetes utility functions (namespace, secret, pod, configmap)
    ├── k8s_session.py          # Session-specific K8s ops (PVC, pod spec, service)
    ├── scheduler.py            # Background cron scheduler for prompt-mode sessions
    ├── models/                 # SQLAlchemy ORM models
    │   ├── __init__.py         # Imports all models (required for Base.metadata)
    │   ├── workspace.py        # Workspace → 1:1 K8s namespace
    │   ├── session.py          # Session (pod lifecycle, modes: tui/server/prompt)
    │   ├── session_repo.py     # Git repos attached to sessions (cloned by init container)
    │   ├── opencode_secret.py  # Fernet-encrypted GCP/Vertex/Gemini credentials
    │   └── github_pat.py       # Fernet-encrypted GitHub PATs
    ├── routers/                # FastAPI route handlers
    │   ├── auth.py             # /login, /logout
    │   ├── workspaces.py       # CRUD for workspaces
    │   ├── sessions.py         # CRUD + launch/stop/status for sessions
    │   ├── secrets.py          # OpenCode secrets, GitHub PATs, pull secrets
    │   ├── chat_proxy.py       # HTTP/WebSocket reverse proxy for server-mode sessions
    │   └── tui_ws.py           # WebSocket PTY proxy for TUI-mode sessions
    └── templates/              # Jinja2 HTML templates (Bootstrap 5 dark theme + HTMX)
        ├── base.html           # Layout with navbar, flash messages, CDN assets
        ├── login.html
        ├── workspaces/         # list, detail, new, edit, _delete_confirm
        ├── sessions/           # list, detail, new, _status_badge, _last_output, _repo_list
        └── secrets/            # tabs, opencode_form, github_pat_form, github_pat_list
```

## Architecture & Key Concepts

### Domain Model

- **Workspace** = 1:1 mapping to a Kubernetes namespace. All resources (sessions, secrets) are scoped to a workspace.
- **Session** = an opencode agent run. Each session creates a K8s Pod + PVC. Three modes:
  - `prompt` — one-shot: runs `opencode run "<prompt>"`, pod exits on completion (`restartPolicy: Never`)
  - `server` — persistent: runs `opencode serve`, creates a ClusterIP Service, dashboard proxies HTTP/WS to it
  - `tui` — persistent: runs `sleep infinity`, user connects via xterm.js WebSocket → kubectl exec PTY
- **Session phases**: `idle` → `pending` → `running` → `succeeded`/`failed`/`stopped`
- **Session scheduling** — prompt-mode sessions can have a cron schedule (`cron_schedule` field, e.g., `*/30 * * * *`). A background asyncio loop (`scheduler.py`) checks every 30s for due sessions and auto-launches them via the shared `_do_launch()` helper in `sessions.py`. Preset cron labels are defined in `CRON_PRESETS` (in `models/session.py`) — shared by the `cron_label` property and the detail template. The scheduler uses an atomic `UPDATE … RETURNING` to claim due rows (prevents duplicates on both SQLite and Postgres).
- **OpencodeSecret** — per-workspace encrypted storage for GCP project, Vertex location, ADC JSON, Google API key
- **GitHubPAT** — per-workspace encrypted GitHub personal access tokens for HTTPS git auth
- **SessionRepo** — git repositories to clone into the session PVC via init containers

### Encryption

All sensitive fields (PATs, API keys, ADC credentials) are Fernet-encrypted at rest in SQLite. The Fernet key is derived from `SHA256("fernet:" + auth_hash_file_contents)`. The session cookie secret uses a separate derivation (`SHA256("session:" + ...)`).

- `crypto.py` must be initialized via `init_crypto()` before any DB access (models call `decrypt()` in property accessors)
- Encrypted fields use `_enc` suffix convention (e.g., `pat_enc`, `google_api_key_enc`)
- Transparent encrypt/decrypt via Python `@property` getters/setters on models

### Database

- **SQLite** via `aiosqlite` + SQLAlchemy 2.x async (`AsyncSession`)
- Database file: `data/swarmer.db` (created automatically on first run)
- Schema created via `Base.metadata.create_all` — no Alembic
- Manual migrations in `database.py:migrate_db()` — uses `ALTER TABLE ... ADD COLUMN` wrapped in try/except (idempotent)
- All models must be imported in `models/__init__.py` for table registration to work

### Kubernetes Integration

- Uses the official `kubernetes` Python client, imported lazily inside functions
- `k8s.init_k8s()` loads either in-cluster or kubeconfig based on `K8S_IN_CLUSTER` setting
- Workspace creation → `ensure_namespace()` + `apply_opencode_config()` (ConfigMap)
- Session launch → `ensure_session_pvc()` + `build_session_pod()` + pod creation
- Pod naming convention: `session-{session_id}-{random_hex_suffix}`
- PVC naming convention: `session-{session_id}-{suffix}` (shared suffix with pod)
- Init container `alpine/git:latest` clones configured repos before main container starts

### UI Pattern

- **Server-rendered HTML** with Jinja2 templates extending `base.html`
- **HTMX** for partial page updates (status polling, inline forms, repo management)
- **Bootstrap 5** dark theme via CDN (no build step, no static assets directory)
- Flash messages stored in Starlette session, rendered in `base.html`
- Templates reference `request`, model objects, and helper functions directly

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
- Auth enforced via `dependencies=[Depends(require_auth)]` on every route
- DB access via `db: AsyncSession = Depends(get_db)`
- POST routes return `RedirectResponse(status_code=302)` (PRG pattern)
- HTMX endpoints return `HTMLResponse` or partial template renders
- Helper functions prefixed with `_` (e.g., `_get_workspace`, `_derive_namespace`)
- Error handling: `IntegrityError` → rollback + re-render form with error message

### Naming Conventions

- Model files: singular noun (`workspace.py`, `session.py`)
- Router files: plural noun matching the resource (`workspaces.py`, `sessions.py`)
- Template directories: plural noun matching the resource
- HTMX partial templates: prefixed with `_` (e.g., `_status_badge.html`, `_repo_list.html`)
- K8s secret names: derived from model fields (e.g., `github-pat-{slug}`, `opencode-secret`)
- URL pattern: `/workspaces/{ws_id}/sessions/{sid}/action`

### Configuration

- `pydantic-settings` with `.env` file support
- All settings have sensible defaults for local development
- Key env vars: `DATABASE_URL`, `AUTH_HASH_FILE`, `K8S_IN_CLUSTER`, `AGENT_IMAGE`, `AGENT_IMAGE_PULL_SECRET`
- Container runtime defaults to `podman` (override with `CONTAINER_CMD=docker`)

## Gotchas & Non-Obvious Patterns

1. **Crypto init order matters**: `init_crypto()` must run before `init_db()` / `create_tables()` because model property accessors call `decrypt()`. The lifespan function in `main.py` enforces this order.

2. **No static files directory**: Despite `app.mount("/static", ...)` in `main.py`, there is no `swarmer/static/` directory. All CSS/JS comes from CDN. The mount will 404 silently if no static files exist. If you add static files, create `swarmer/static/` first.

3. **Deployment image placeholder**: `k8s/swarmer/deployment.yaml` uses the literal string `SWARMER_IMAGE` which is replaced at deploy time via `sed` in the Makefile. Don't replace it with an actual image reference.

4. **SQLite single-writer**: The K8s Deployment uses `strategy: Recreate` (not RollingUpdate) because SQLite doesn't support concurrent writers. Only one replica is safe.

5. **Session mode affects pod lifecycle**:
   - `prompt` mode: `restartPolicy: Never`, pod exits after opencode finishes
   - `server`/`tui` modes: `restartPolicy: Always`, pod runs indefinitely
   - Stopping a session always deletes the pod; if `persist=False`, the PVC is also deleted

6. **Port-forward management**: `chat_proxy.py` maintains a global dict of `kubectl port-forward` subprocesses (`_portforwards`). These are reused across requests and cleaned up when the pod changes. In dev mode (`K8S_IN_CLUSTER=false`), the browser is redirected directly to `localhost:{port}` instead of proxying.

7. **TUI auth tokens**: TUI WebSocket connections are authenticated with one-time UUID tokens stored in the HTTP session. Tokens are generated on the session detail page and consumed on WebSocket connect.

8. **Model selection state file**: opencode's model selection is persisted by writing `/root/.local/state/opencode/model.json` inside the pod via a shell preamble before the main command runs. The `exec_model_json()` function in `k8s.py` can update this on a running pod.

9. **opencode share directory**: Session history is symlinked from `/workspace/.opencode` → `/root/.local/share/opencode` so it persists across pod restarts when the PVC is retained.

10. **Manual migrations**: New columns are added via `database.py:migrate_db()` with `ALTER TABLE` statements. Only "duplicate column" / "already exists" errors are suppressed; other failures re-raise so startup fails visibly. When adding a new column to an existing table, add the migration there and include a `server_default` so existing rows work.

11. **Blocking K8s calls in async handlers**: All synchronous `kubernetes` client calls inside async functions must be wrapped with `asyncio.to_thread()` to avoid blocking the event loop. This includes `ensure_session_pvc`, `create_namespaced_pod`, `delete_pod`, `create_session_service`, `create_session_route`, and `_grant_anyuid_scc`.

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
3. Sync to K8s Secret in `k8s.py` using `_apply_secret()` helper
