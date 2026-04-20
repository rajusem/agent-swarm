# agent-swarm

A FastAPI + HTMX dashboard for managing [opencode](https://opencode.ai) agent workloads on Kubernetes.

## Capabilities

- **Workspaces** — each workspace maps 1:1 to a Kubernetes namespace; create, rename, and delete workspaces from the UI
- **Secrets** — Fernet-encrypted storage for OpenCode credentials (GCP/Vertex AI, Gemini), GitHub PATs for HTTPS git auth, and OCI registry pull secrets; all auto-synced to Kubernetes Secret objects
- **Session lifecycle** — create → launch → monitor → stop → delete sessions backed by Kubernetes Pods and PVCs
- **Three session modes:**
  - **Prompt** — one-shot: run a prompt, stream output, pod exits when done
  - **Server** — persistent opencode web API with in-dashboard chat link
  - **TUI** — full xterm.js browser terminal connected via WebSocket + `kubectl exec` PTY
- **Git cloning** — init containers clone configured repos into the PVC-backed workspace before the agent starts
- **Live UI** — HTMX polling for session status and output; no page reloads needed

## Prerequisites

- Python 3.11+ and `pip`
- `kubectl`
- `kind` (for local cluster modes)
- Docker or Podman (`CONTAINER_CMD=podman` to use Podman)
- `opencode-golang:latest` container image available locally

## Running

### Option 1: kind cluster + `make dev` (hybrid — hot reload)

Best for active Python development. FastAPI runs locally with auto-reload; session pods run inside kind.

```sh
make setup-auth          # set dashboard password
make install             # pip install -r requirements.txt
make kind-create         # create kind cluster (localhost:8080 → NodePort 30080)
make kind-load-opencode  # load opencode agent image into kind
make dev                 # uvicorn at http://localhost:8090, K8S_IN_CLUSTER=false
```

Dashboard: http://localhost:8090

### Option 2: kind (fully containerized)

Best for end-to-end local testing. One command builds the image, creates the cluster, and deploys everything.

```sh
make setup-auth    # set dashboard password
make kind-deploy   # create cluster + build image + load + deploy (idempotent)
```

Dashboard: http://localhost:8080 (via NodePort — no port-forward needed)

Teardown:
```sh
make kind-delete   # deletes the kind cluster and all data inside it
```

### Option 3: Real Kubernetes cluster

Push the image to a registry and deploy to your current `kubectl` context.

```sh
make setup-auth
make image-build image-push REGISTRY=your.registry.example.com
make k8s-deploy    # applies namespace, RBAC, PVC, service, deployment
make k8s-connect   # port-forward → http://localhost:8080
```

Teardown:
```sh
make k8s-delete    # removes all swarmer resources from the namespace
```

## Configuration

Copy `.env.example` to `.env` and adjust as needed:

```sh
cp .env.example .env
```

Key variables:

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///data/swarmer.db` | SQLite database path |
| `AUTH_HASH_FILE` | `auth/password.hash` | Argon2 password hash (written by `make setup-auth`) |
| `K8S_IN_CLUSTER` | `false` | Set to `true` when running inside a pod |
| `AGENT_IMAGE` | `opencode-golang:latest` | Image used for session pods |
| `AGENT_IMAGE_PULL_SECRET` | _(empty)_ | Pull secret name in the workspace namespace |

## Access Control

> **These two commands are the primary way to onboard users and control workspace access.**

### Issue a login token

Creates a Kubernetes ServiceAccount for the user (if it doesn't exist) and prints a bearer token they paste into the Swarmer login page:

```sh
make user-token SA_USER=alice
make user-token SA_USER=alice TOKEN_DURATION=24h   # default: 8h
```

Share the printed token with the user — it expires after `TOKEN_DURATION`.

### Grant workspace access

Binds a user to a specific workspace namespace so they can see and manage sessions in it:

```sh
make grant-workspace SA_USER=alice WORKSPACE_NS=my-project
```

Run this once per user per namespace. A user with no workspace grants can log in but will see no workspaces.

### Typical onboarding flow

```sh
make user-token SA_USER=alice                          # 1. create user + print token
make grant-workspace SA_USER=alice WORKSPACE_NS=team-a # 2. give access to a workspace
make grant-workspace SA_USER=alice WORKSPACE_NS=team-b # 3. repeat for additional workspaces
```

## Other useful targets

```sh
make help          # list all Makefile targets
make lint          # run ruff linter
make db-reset      # delete the SQLite database (fresh schema on next start)
```
