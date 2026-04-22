# Auth Model Improvement: K8s Bearer Token Auth

**Version shipped**: 0.1  
**Date**: 2026-04-20

---

## Context

The original auth system used a single shared Argon2 password hash stored in `auth/password.hash`. Problems:
- No multi-user access control — everyone shared one password and saw everything
- The crypto key for GitHub PATs and session cookies was derived from the hash, coupling auth to crypto
- OpenShift users already have a K8s bearer token from the console ("Copy login command") that could authenticate them directly
- K3s/Kind users have kubeconfig tokens that work the same way at the K8s API level

The goal was a single token-based auth system: user presents a K8s bearer token (from OpenShift OAuth or pasted from kubeconfig), the app validates it via the K8s API, and uses it to filter which workspaces/sessions the user can see. The swarmer service account continues to perform actual K8s operations; the user token is only used for auth and visibility filtering.

---

## 1. OpenShift OAuth Support

### Architecture

**OpenShift**:
1. If `OPENSHIFT_OAUTH_URL` is configured, show a "Sign in with OpenShift" button
2. Button redirects to OpenShift OAuth implicit flow (`response_type=token`)
3. OpenShift redirects back to `/auth/callback#access_token=<token>`
4. A JS page extracts the token from the URL fragment and POSTs it to `/auth/callback`
5. Same validation pipeline as token paste

**K3s/Kind**:
1. User navigates to `/login`, pastes their K8s bearer token (from kubeconfig or SA token)
2. Same validation pipeline as OpenShift

### Validation pipeline (unified)
1. **TokenReview** — swarmer service account POSTs to `authentication.k8s.io/v1/tokenreviews` to verify the token is authentic. Falls back to namespace GET probe if 403 (RBAC not yet applied).
2. **Namespace access check** — one-off client built with the user's token checks which registered workspace namespaces they can GET
3. Login succeeds if token is valid AND at least one namespace is accessible (empty list treated as "all accessible")
4. Token is encrypted (Fernet) and stored in session as `k8s_token`
5. `workspace_list` uses the token to filter visible workspaces on every request (live, per-request)

### What was built

- **`swarmer/k8s_auth.py`** — `TokenIdentity`, `validate_token()`, `get_accessible_namespaces()` using a one-off K8s ApiClient built with the user's bearer token. `validate_token` uses `TokenReview` first, falls back to a namespace GET probe if the swarmer SA lacks `tokenreviews/create` RBAC.
- **`swarmer/crypto.py`** — Key loaded from `SWARMER_SECRET_KEY` env var → `auth/secret.key` file → auto-generated random 32 bytes. `decrypt()` catches `InvalidToken` and returns `""` rather than crashing (safe for key rotation / old encrypted values).
- **`swarmer/auth.py`** — Replaced Argon2 password auth with token-based auth. Login validates the bearer token via `k8s_auth.validate_token` and stores encrypted token in the session cookie.
- **`swarmer/routers/auth.py`** — `GET /login` renders login page with optional "Sign in with OpenShift" button. `POST /login` accepts pasted token. `GET /auth/callback` serves the JS extraction page. `POST /auth/callback` validates the OpenShift OAuth token. `POST /logout` clears the session.
- **`swarmer/templates/auth_callback.html`** — JS page that reads the access token from the `#access_token=` URL fragment (OpenShift implicit flow) and POSTs it to `/auth/callback`.
- **`swarmer/templates/login.html`** — Token paste form plus optional "Sign in with OpenShift" button controlled by the `OPENSHIFT_OAUTH_URL` env var.
- **`k8s/openshift/oauth-client.yaml`** — `OAuthClient` CR for OpenShift, `response_type: token` (implicit flow), `grantMethod: auto`.

### OAuth redirect URI fix

OpenShift rejected the redirect URI with `invalid_request` until two fixes were applied:

1. **`--proxy-headers --forwarded-allow-ips=*`** added to the uvicorn `CMD` in `Containerfile` so that the `https://` scheme is correctly detected behind the OpenShift edge-TLS Route.
2. **`urllib.parse.quote(str(callback_url), safe='')`** applied to the `redirect_uri` query parameter so the URL is percent-encoded correctly.

### Post-OAuth redirect fix

After OAuth validation succeeded, users were redirected back to `/login` because `get_accessible_namespaces` returned an empty list when no workspaces existed yet in the DB. Fixed with a `if namespaces:` guard — an empty workspace list is treated as "all accessible" rather than "access denied".

---

## 2. Breaking Change: Crypto Key Migration

Previously `crypto.py` derived both the Fernet key and session signing key from `auth/password.hash` bytes. With password auth removed, those keys come from elsewhere.

**New key source**: `SWARMER_SECRET_KEY` env var (32 bytes, base64-encoded), OR auto-generated and persisted to `auth/secret.key` on first startup.

**Key resolution order**:
1. `SWARMER_SECRET_KEY` env var (base64-encoded 32 bytes)
2. `secret_key_file` on disk (default `auth/secret.key`)
3. Generate 32 random bytes, persist to `secret_key_file`, log warning

**Impact**: All encrypted GitHub PATs and OpenCode secrets stored in the DB became unreadable (different key). Users must re-enter them. `decrypt()` catches `InvalidToken` and returns `""` with a warning log rather than a hard 500.

---

## 3. OpenShift Pod Security (SCC)

Session pods run as `runAsUser: 0` (required by the agent tool images). On OpenShift, pods must be explicitly granted the `anyuid` SCC or they are rejected by the admission controller.

### `k8s.py` — `_grant_anyuid_scc(namespace)`

Called from `ensure_namespace()` every time a workspace namespace is created. Creates a `ClusterRoleBinding` named `swarmer-anyuid:<namespace>` binding the namespace's `default` SA to `system:openshift:scc:anyuid`.

- Status `409` → already exists, skip.
- Status `403` or `404` → not OpenShift (kind/k3s) or missing ClusterRole, skip silently.

### `k8s_session.py` — pod security context

`run_as_user=0` is always set. `privileged=True` is set only when `session.privileged` is explicitly checked. Previously both were coupled, causing sessions without the privileged flag to still request `privileged: true`, which `restricted-v2` SCC rejects.

### RBAC additions (`k8s/swarmer/rbac.yaml`)

| Resource | Verbs | Reason |
|----------|-------|--------|
| `configmaps` | get/create/update/patch/delete | Agent tool ConfigMap per workspace namespace |
| `clusterrolebindings` | get/create/update/patch/delete | Grant anyuid SCC to workspace namespace default SA |
| `authentication.k8s.io/tokenreviews` | create | Validate user bearer tokens at login |
| `route.openshift.io/routes` | get/list/watch/create/delete | Expose server-mode sessions directly to the browser |

---

## 4. TUI WebSocket Proxy — kubectl exec → kubernetes.stream()

The original TUI proxy (`tui_ws.py`) spawned a `kubectl exec -it` subprocess using a PTY. This failed in-cluster on OpenShift because the SPDY/WebSocket exec protocol used by the kubectl subprocess does not work reliably through OpenShift's API proxy and edge route.

### Replacement: `kubernetes.stream()` in a background thread

```python
from kubernetes.stream import stream as k8s_stream

exec_resp = k8s_stream(
    v1.connect_get_namespaced_pod_exec,
    pod_name, namespace,
    container=container_name,
    command=["sh", "-c", tui_shell],
    stderr=True, stdin=True, stdout=True, tty=True,
    _preload_content=False,
)
```

A daemon thread runs `exec_resp.update(timeout=0.1)` in a loop, pushing stdout/stderr bytes into an `asyncio.Queue`. The event loop reads the queue and forwards bytes to the browser's xterm.js as binary WebSocket frames.

Browser keystrokes (`msg["bytes"]`) are written via `exec_resp.write_channel(0, data)`. Terminal resize events go to channel 4 as `json.dumps({"Width": cols, "Height": rows})`.

This approach uses the in-cluster service account credentials directly (loaded by `init_k8s()`), avoiding all kubectl subprocess issues. Works identically on kind/k3s.

### Pod startup fix — git not found

The container startup script ran `git config --global ...` unconditionally when `GITHUB_PAT` was set. The opencode container image does not have git in its PATH. This caused the startup script to fail before `share_setup` could run `mkdir -p /workspace/.opencode`, which then caused opencode's own `auth.json` write to fail.

**Fix in `k8s_session.py`**:
```python
'if [ -n "${GITHUB_PAT}" ] && command -v git >/dev/null 2>&1; then '
```
Git config is skipped silently when git is not available.

---

## 5. Server Mode Chat — OpenShift Route per Session

The original server-mode chat proxy used `kubectl port-forward` subprocess on the swarmer pod, then forwarded HTTP/WebSocket traffic at `/workspaces/{ws_id}/sessions/{sid}/chat/{path}`. This had two problems:

1. `kubectl port-forward` subprocess failed in-cluster on OpenShift (same SPDY issue as TUI exec).
2. Even when the HTTP proxy worked, opencode's SPA makes absolute-path `fetch('/api/...')` and WebSocket calls that go to the swarmer host instead of through the sub-path proxy.

### Replacement: per-session OpenShift Route

When a server-mode session is launched, swarmer creates an OpenShift `Route` named `session-{id}-chat` in the workspace namespace pointing to the session's ClusterIP service (`session-{id}-svc`):

```python
# k8s.py
custom.create_namespaced_custom_object(
    group="route.openshift.io", version="v1", namespace=namespace,
    plural="routes",
    body={
        "spec": {
            "to": {"kind": "Service", "name": service_name},
            "port": {"targetPort": port},
            "tls": {"termination": "edge", "insecureEdgeTerminationPolicy": "Redirect"},
        }
    },
)
```

OpenShift auto-assigns a hostname (`session-{id}-chat-{namespace}.apps.{cluster-domain}`). When the user clicks "Open Web Chat", swarmer reads the Route's `status.ingress[0].host` and redirects the browser directly to `https://{host}/`. The SPA then runs at root with no sub-path mangling — all absolute-path API calls and WebSocket connections work correctly.

The Route is deleted when the session is stopped.

**Falls back gracefully on non-OpenShift**: `create_session_route` and `delete_session_route` return silently on status 403/404 (kind/k3s where the CRD does not exist). `chat_proxy.py` falls back to the sub-path HTTP proxy when no Route hostname is found.

**In-cluster HTTP proxy** (fallback path): updated to use the service ClusterIP DNS (`session-{id}-svc.{namespace}.svc.cluster.local:{port}`) directly instead of kubectl port-forward, so at least static assets work even when the Route is unavailable.

---

## 6. Status Badge Auto-Update

The HTMX status badge (`_status_badge.html`) polls `/workspaces/{ws_id}/sessions/{sid}/status` every 5 seconds. The `session_status` route previously read the phase directly from the DB without querying K8s.

For TUI and server mode sessions the log_poller was not started (only prompt mode), so the DB phase stayed "pending" indefinitely. The JS handler in `detail.html` that triggers the TUI page reload or shows the "Open Web Chat" button checks `badge.classList.contains('bg-success')`, which never became true.

### Fix: `session_status` syncs phase from K8s

```python
if session.pod_name and session.phase in ("pending", "running"):
    live_phase, live_detail = await asyncio.to_thread(
        k8s.get_pod_status, session.pod_name, ws.k8s_namespace
    )
    if live_phase != session.phase or live_detail != session.status_detail:
        session.phase = live_phase
        session.status_detail = live_detail
        await db.commit()
```

Once the pod reaches "running", the badge shows "● Active" (`bg-success`), the JS detects it, and:
- **TUI**: `window.location.reload()` causes the server to render the terminal card with a fresh one-time token.
- **Server**: `chatBtn.classList.toggle('d-none', false)` reveals the "Open Web Chat" button.

The log_poller is also now started for server mode sessions (not TUI — the TUI terminal handles interaction directly) so startup logs appear in the Last Output panel.

---

## 7. Containerfile Changes

```dockerfile
# kubectl for TUI exec (still used for dev-mode port-forward)
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
    KUBECTL_VERSION=$(curl -sSL https://dl.k8s.io/release/stable.txt) && \
    curl -sSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
      -o /usr/local/bin/kubectl && chmod +x /usr/local/bin/kubectl && \
    apt-get remove -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

CMD ["uvicorn", "swarmer.main:app", "--host", "0.0.0.0", "--port", "8080",
     "--proxy-headers", "--forwarded-allow-ips=*"]
```

`--proxy-headers` is required for correct `https://` scheme detection behind the OpenShift edge-TLS Route (used to build the OAuth redirect URI).

---

## 8. Makefile / VERSION

- `VERSION` file at repo root stores the image tag (`0.9` after this work).
- `image-build` reads `VERSION`, prompts for an update (skip with `SILENT=1`), writes back if changed.
- `image-push` always reads `VERSION` — no prompting.
- `sync-images` syncs agent image tags from `../agent-containers/.push-defaults` before each build.

### Token issuance for Kind/K3s

Kind and K3s don't have a self-service OAuth flow, so users need an admin to generate a token.

**`make user-token SA_USER=<name> [TOKEN_DURATION=8h]`** — creates the ServiceAccount in the swarmer namespace if it doesn't exist, then prints a time-bounded token for the user to paste into the login form.

**`make grant-workspace SA_USER=<name> WORKSPACE_NS=<namespace>`** — binds the user's ServiceAccount to the workspace namespace so `get_accessible_namespaces` returns it. Uses the `swarmer-user` ClusterRole (GET namespaces, GET/list pods).

Typical admin workflow:
```bash
make user-token SA_USER=alice           # creates SA, prints token
make grant-workspace SA_USER=alice WORKSPACE_NS=team-a-project
# Alice pastes the token → sees only team-a-project
```

---

## Implementation Details

### `swarmer/k8s_auth.py`

```python
@dataclass
class TokenIdentity:
    username: str
    uid: str = ""
    groups: list[str] = field(default_factory=list)

async def validate_token(token, api_url, in_cluster) -> TokenIdentity | None:
    # Step 1: swarmer SA POSTs tokenreviews to verify authenticity
    # Falls back to namespace GET if 403 on tokenreviews (RBAC not applied yet)

async def get_accessible_namespaces(token, namespaces, api_url, in_cluster) -> list[str]:
    # Builds one-off ApiClient with user token; GETs each namespace; returns accessible subset

async def can_create_pods(token, namespace, api_url, in_cluster) -> bool:
    # SelfSubjectAccessReview with user token for pods/create in namespace
```

One-off user client (no global state mutation):
```python
cfg = k8s_client.Configuration()
cfg.host = api_url  # or https://kubernetes.default.svc in-cluster
cfg.api_key = {"authorization": f"Bearer {token}"}
cfg.ssl_ca_cert = INCLUSTER_CA  # /var/run/secrets/kubernetes.io/serviceaccount/ca.crt if in_cluster
```

All sync K8s calls wrapped with `asyncio.to_thread`.

### `swarmer/routers/auth.py` endpoints

- `GET /login` — renders `login.html` with optional `openshift_auth_url`
- `POST /login` — validate token via `k8s_auth.validate_token` + `get_accessible_namespaces`; set `session["k8s_token"] = encrypt(token)` + `session["authenticated"] = True`
- `GET /auth/callback` (name=`oauth_callback`) — renders `auth_callback.html` (JS extraction page)
- `POST /auth/callback` — identical validation as `POST /login`
- `POST /logout` — `session.clear()`

OpenShift redirect URL built in `GET /login`:
```python
f"{settings.openshift_oauth_url}/oauth/authorize"
f"?client_id=swarmer&response_type=token"
f"&redirect_uri={urllib.parse.quote(str(callback_url), safe='')}"
```

### `swarmer/routers/workspaces.py` — `workspace_list` change

```python
# after fetching all_workspaces from DB:
user_token = get_user_token(request)
ns_to_ws = {ws.k8s_namespace: ws for ws in all_workspaces}
accessible_ns = await k8s_auth.get_accessible_namespaces(
    user_token, list(ns_to_ws), settings.k8s_api_url, settings.k8s_in_cluster
)
workspaces = sorted([ns_to_ws[ns] for ns in accessible_ns], key=lambda w: w.display_name)
```

### `k8s/openshift/oauth-client.yaml` (OpenShift only)

```yaml
apiVersion: oauth.openshift.io/v1
kind: OAuthClient
metadata:
  name: swarmer
redirectURIs:
  - https://swarmer.apps.your-cluster.example.com/auth/callback
  - http://localhost:8080/auth/callback
grantMethod: auto
responseTypes:
  - token
```

---

## Compatibility

All changes preserve kind/k3s compatibility:

| Feature | OpenShift | kind/k3s |
|---------|-----------|----------|
| OAuth login | OpenShift implicit flow | Token paste only |
| anyuid SCC grant | Creates ClusterRoleBinding | Silently skips (404) |
| TUI exec | `kubernetes.stream()` | `kubernetes.stream()` (same) |
| Server mode chat | OpenShift Route → direct browser access | Sub-path HTTP proxy fallback |
| Status badge | K8s sync in `/status` poll | K8s sync in `/status` poll (same) |

---

## Design Decisions

- **Workspace visibility**: Live per-request — `workspace_list` calls `get_accessible_namespaces` on every load. Accurate but adds ~1 K8s API call per workspace per page load. Acceptable at current scale.
- **Password fallback**: Removed entirely. No Argon2 / hash file path anywhere in the new code.
- **TokenReview fallback**: If swarmer SA gets 403 on `tokenreviews/create` (RBAC not yet applied), fall back to probing a namespace GET with the user token directly. Logs a warning so operators know to apply updated RBAC.
- **Empty namespace list**: Treated as "all accessible" rather than "access denied" — prevents lockout when a fresh deployment has no workspaces yet.

---

## Verification

1. **K3s/Kind token paste**: `kubectl config view --raw -o jsonpath='{.users[0].user.token}'` → paste → should land on `/workspaces` showing only accessible namespaces
2. **Invalid token**: submit garbage → 401 error on login page
3. **No namespace access**: token from SA with no RBAC → login rejected with "no access to any workspace namespace"
4. **OpenShift OAuth**: click button → redirect → OpenShift login → `/auth/callback` → `/workspaces`
5. **RBAC check**: `kubectl auth can-i create tokenreviews --as=system:serviceaccount:swarmer:swarmer` → `yes`
6. **Crypto migration**: old encrypted PATs return `""` (not 500) after key rotation; re-entering saves correctly
7. **TUI exec**: attach to a running TUI session — keystrokes and terminal resize work in-cluster
8. **Server mode (OpenShift)**: launch server session → Route hostname appears → "Open Web Chat" opens SPA at root path
9. **Status badge**: badge transitions from "Pending" to "● Active" without manual refresh
