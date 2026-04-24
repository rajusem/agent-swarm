# Plan: OpenShift Deploy & SCC Fixes
**Date:** 2026-04-23
**Branch:** main

## Context
The swarmer OpenShift deployment had several issues discovered during a live deploy:
1. The system consolidated from two agent images (`opencode-golang`, `opencode-python`) to a single `quay.io/jpacker/opencode:0.1.1` image, but many references to the old images remained.
2. The `make openshift-deploy` target had a stale `AGENT_IMAGE_PYTHON_VALUE` sed substitution that caused the deployment yaml not to apply correctly.
3. The OAuthClient yaml contained an invalid `responseTypes` field causing a strict decoding error on apply.
4. Session pod launches were failing with SCC 403: `unable to validate against any security context constraint`. The root cause was that `_grant_anyuid_scc` was creating a **ClusterRoleBinding**, but OpenShift 4.x uses a namespace-scoped **RoleBinding** for SCC grants (matching what `oc adm policy add-scc-to-user` does). The CRB was being silently swallowed as 403/404 and the pod's `run_as_user=0` then had no valid SCC.

## Approach

### Image consolidation
- Replace all `opencode-golang:0.2` / `opencode-python:0.2` references with `quay.io/jpacker/opencode:0.1.1`.
- Remove `AGENT_IMAGE_PYTHON` env var from the OpenShift deployment yaml and all sed/env/config references.
- Update `swarmer/config.py` to drop `agent_image_python` field.
- Update kustomize overlays to remove the duplicate `AGENT_IMAGE_PYTHON` patch.

### Deploy target fix
- Remove the `AGENT_IMAGE_PYTHON_VALUE` substitution from the `openshift-deploy` sed command in the Makefile.
- Remove `AGENT_IMAGE_PYTHON` variable from Makefile defaults.
- Update `sync-images` and `kind-load-opencode` targets to use the new single `opencode` image name.

### OAuthClient fix
- Remove the invalid `responseTypes: [token]` field from `k8s/openshift/oauth-client.yaml`.

### SCC grant fix
- Rewrite `_grant_anyuid_scc` in `swarmer/k8s.py` to create a namespace-scoped `RoleBinding` instead of a `ClusterRoleBinding`.
- Update `k8s/swarmer/rbac.yaml`: replace `clusterrolebindings` permission with `rolebindings`, remove the `bind` verb rule on anyuid ClusterRole.
- Add `_grant_anyuid_scc` call at session launch time in `sessions.py` as a retry safety net (in case the workspace was created before the RBAC fix was in place).
- Upgrade 403 log level from `debug` to `warning` with full error body for visibility.

## Files to Change
- `k8s/openshift/deployment.yaml` — remove `AGENT_IMAGE_PYTHON` env var
- `k8s/openshift/oauth-client.yaml` — remove invalid `responseTypes` field
- `k8s/swarmer/rbac.yaml` — replace clusterrolebindings with rolebindings; remove bind rule
- `install-agent-swarm.md` — update image, remove python references, fix sed command
- `Makefile` — update image defaults, remove AGENT_IMAGE_PYTHON, fix sync-images and kind-load-opencode
- `kustomize/overlays/ephemeral/kustomization.yaml` — remove AGENT_IMAGE_PYTHON patch, update image
- `swarmer/config.py` — remove `agent_image_python` field
- `swarmer/k8s.py` — rewrite `_grant_anyuid_scc` to use RoleBinding
- `swarmer/routers/sessions.py` — add `_grant_anyuid_scc` retry before pod creation
- `.env` — update `AGENT_IMAGE_OPENCODE` to new image tag

## Verification
```bash
# Redeploy and confirm clean apply (no sed errors, no OAuthClient errors)
make openshift-deploy

# Confirm anyuid RoleBinding is created in workspace namespace on session launch
kubectl get rolebinding -n <workspace-ns> | grep anyuid

# Confirm session pod starts
kubectl get pods -n <workspace-ns>
```

---
## Implementation Summary

All four fix areas were completed on branch `main` (2026-04-23).

### Image consolidation
Removed all `opencode-golang:0.2` / `opencode-python:0.2` references. The deployment yaml now has only two agent image env vars — `AGENT_IMAGE_OPENCODE` and `AGENT_IMAGE_CRUSH` — with `AGENT_IMAGE_PYTHON` deleted from the template and all sed substitution targets. `swarmer/config.py` had `agent_image_python` removed from `Settings`; the field was already `extra: ignore` so no migration was needed. The kustomize ephemeral overlay was updated to remove the stale `AGENT_IMAGE_PYTHON` patch. Makefile `AGENT_IMAGE_PYTHON` default and `AGENT_IMAGE_PYTHON_VALUE` sed target were removed; `sync-images` and `kind-load-opencode` targets updated to use the consolidated `opencode` image name.

### Deploy target fix
Removed the `AGENT_IMAGE_PYTHON_VALUE` sed substitution from the `openshift-deploy` Makefile target. The target now only substitutes `SWARMER_IMAGE`, `OPENSHIFT_OAUTH_URL`, `AGENT_IMAGE_OPENCODE_VALUE`, and `AGENT_IMAGE_CRUSH_VALUE`.

### OAuthClient fix
Removed the invalid `responseTypes: [token]` field from `k8s/openshift/oauth-client.yaml`. The file now contains only `redirectURIs` and `grantMethod: auto`, which applies cleanly with `kubectl apply --server-side`.

### SCC grant fix
- **`swarmer/k8s.py`**: Rewrote `_grant_anyuid_scc` to call `rbac.create_namespaced_role_binding(namespace, rb)` with a `V1RoleBinding` (namespace-scoped) instead of `create_cluster_role_binding`. The `roleRef` still points at `ClusterRole/system:openshift:scc:anyuid`, which is the correct OpenShift 4.x pattern matching `oc adm policy add-scc-to-user`. 403 responses are now logged at `warning` level with the full `exc.body`.
- **`k8s/swarmer/rbac.yaml`**: Replaced the `clusterrolebindings` resource in the swarmer ClusterRole with `rolebindings`, and removed the separate `bind` verb rule on the anyuid ClusterRole. The comment on the rule explains the OpenShift 4.x rationale.
- **`swarmer/routers/sessions.py`**: Added a `_grant_anyuid_scc` retry call at `session_launch` time (before pod creation), guarded by `if not settings.k8s_namespace` so it only runs in per-workspace-namespace mode. This handles workspaces created before the RBAC fix was deployed.
