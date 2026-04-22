# 2026-04-21 — CodeRabbit PR #12 Fixes

## Context

PR #12 (feat/k8s-token-auth) was reviewed by CodeRabbit. This plan documents the issues selected for immediate remediation and what was implemented.

## Issues Selected for Fixing

CodeRabbit flagged 13 inline comments across the PR. The following were actioned:

### Must Fix (security / correctness blockers)

| File | Issue |
|------|-------|
| `swarmer/deps.py` | Empty `decrypt()` result returned as valid token |
| `swarmer/templates/auth_callback.html` | Token truncation at `=` in manual fragment parser |
| `swarmer/templates/auth_callback.html` | Missing OAuth `state` parameter — CSRF vulnerability |
| `swarmer/routers/sessions.py` | Blocking `k8s.get_pod_status` calls inside async handlers |

### Fix Before Merge (functional correctness)

| File | Issue |
|------|-------|
| `k8s/openshift/deployment.yaml` | Hardcoded image tags instead of `sed` placeholders expected by install guide |

### Additional Improvements

| File | Change |
|------|--------|
| `Makefile` | `k8s-deploy` prints the dashboard URL after rollout (Route → NodePort → port-forward hint) |

---

## Implementation

### `swarmer/deps.py` — Fail closed on empty decrypted token

`get_user_token` previously returned whatever `decrypt()` produced, including an empty string when the Fernet key had been rotated or the ciphertext was invalid. An empty token would be forwarded to K8s API calls, causing confusing downstream failures rather than a clean 401.

```python
# before
return decrypt(request.session.get("k8s_token", ""))

# after
token = decrypt(request.session.get("k8s_token", ""))
if not token:
    raise NotAuthenticated()
return token
```

### `swarmer/routers/sessions.py` — Async-safe K8s status calls

`k8s.get_pod_status` is synchronous (uses the blocking kubernetes-client). It was called inline in two async route handlers, blocking the uvicorn event loop on every page load or list refresh when a session was active. The fix wraps both call sites with `asyncio.to_thread`, matching the pattern already used in `session_status`.

Affected locations:
- `session_list` — per-session phase sync loop
- `session_detail` — initial page render phase sync

```python
# before
live_phase, _ = k8s.get_pod_status(s.pod_name, ws.k8s_namespace)

# after
live_phase, _ = await asyncio.to_thread(k8s.get_pod_status, s.pod_name, ws.k8s_namespace)
```

### `swarmer/templates/auth_callback.html` + `swarmer/routers/auth.py` — OAuth state (CSRF) + token parsing

**Token parsing fix:** The original JS split the URL fragment on `&`, then split each pair on `=` taking only `parts[1]`. This silently truncates base64 values that contain `=` padding (common in JWTs). Replaced with `URLSearchParams` which handles `=` correctly.

```js
// before
var params = {};
hash.split('&').forEach(function(pair) {
  var parts = pair.split('=');
  params[decodeURIComponent(parts[0])] = decodeURIComponent(parts[1] || '');
});
var token = params['access_token'];

// after
var params = new URLSearchParams(window.location.hash.slice(1));
var token = params.get('access_token') || '';
```

**OAuth state / CSRF fix:** The implicit flow had no `state` parameter, leaving the callback open to login-CSRF (attacker tricks a victim's browser into completing an OAuth flow with the attacker's token). Three-part change:

1. `login_page()` generates a `secrets.token_urlsafe(16)` state, stores it in the session, and appends `&state=<value>` to the authorization URL.
2. `auth_callback.html` extracts `state` from the fragment via `URLSearchParams` and includes it as a hidden form field in the POST.
3. `oauth_callback()` accepts `state: str = Form("")`, pops `oauth_state` from the session, and rejects the request if the values don't match.

```python
# login_page — generate and store state
state = secrets.token_urlsafe(16)
request.session["oauth_state"] = state
openshift_auth_url = (
    f"...&state={state}"
)

# oauth_callback — validate state
expected = request.session.pop("oauth_state", None)
if not expected or state != expected:
    flash(request, "Invalid OAuth state. Please sign in again.", "error")
    return RedirectResponse("/login", ...)
```

### `k8s/openshift/deployment.yaml` — Install-guide placeholders

The deployment manifest had hardcoded image references that broke the `sed` substitutions in `install-agent-swarm.md` Step 8. Replaced with the placeholder tokens the guide expects:

| Field | Before | After |
|-------|--------|-------|
| `containers[0].image` | `quay.io/jpacker/swarmer:0.1` | `SWARMER_IMAGE` |
| `AGENT_IMAGE_OPENCODE` value | `quay.io/jpacker/opencode-golang:0.2` | `AGENT_IMAGE_OPENCODE_VALUE` |
| `AGENT_IMAGE_PYTHON` value | `quay.io/jpacker/opencode-python:0.2` | `AGENT_IMAGE_PYTHON_VALUE` |
| `AGENT_IMAGE_CRUSH` value | `ghcr.io/gurnben/crush-container:latest` | `AGENT_IMAGE_CRUSH_VALUE` |

### Lint fixes (`make lint`)

After the above changes `ruff check swarmer/` reported two additional issues:

1. **`swarmer/routers/auth.py` F401** — `decrypt` was imported but no longer used (only `encrypt` is called in `_validate_and_login`). Removed from the import line.

2. **`swarmer/routers/sessions.py` E402** — `log = logging.getLogger(__name__)` was positioned between stdlib imports and third-party imports, causing ruff to flag every import below it as "not at top of file". Moved the logger assignment to after the full import block.

### `Makefile` — `k8s-deploy` prints the dashboard URL

Previously the target ended with a static hint to run `make k8s-connect`. Now it detects the correct URL and prints it directly after the rollout completes:

1. **OpenShift Route** — `kubectl get route swarmer` extracts `.spec.host`; prints `https://<host>`.
2. **Node IP + NodePort** — reads the first node's ExternalIP (falling back to InternalIP) and prints `http://<ip>:30080`.
3. **Port-forward fallback** — if neither lookup yields an address, prints the original `make k8s-connect` message.

---

## Deferred (will not fix in this PR)

- `Containerfile` kubectl version pinning + checksum (supply-chain hygiene, follow-up PR)
- `--forwarded-allow-ips=*` in Containerfile CMD (reasonable for OpenShift route topology; can be made configurable later)
- `k8s/openshift/oauth-client.yaml` localhost redirect URI in prod manifest (low practical risk for internal deployment)
- `scripts/setup_auth.py` silent no-op (cosmetic; deprecated file)
- All nitpicks (adaptive polling, badge macro dedup, `async for … break` in `main.py`, type hints, ApiClient reuse)
