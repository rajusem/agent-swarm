---
name: openshell-deploy
description: Use when installing, configuring, or redeploying OpenShell for the Swarmer project — covers both local kind clusters and OpenShift clusters, SCC fixes, cert extraction and validation, secret creation, and wiring the deployment manifest.
---

# OpenShell Deploy Skill

Covers end-to-end OpenShell setup for Swarmer — from Helm install to a running
pod with correct mTLS certs. Handles both local kind and OpenShift targets.

## Key Makefile targets

| Target | What it does |
|---|---|
| `make openshell-setup` | Install Agent Sandbox CRDs + Helm chart into `openshell` namespace, extract certs, create `openshell-tls` secret in `$(NAMESPACE)` |
| `make openshell-extract-tls` | Pull mTLS certs from cluster secret → `auth/openshell/` |
| `make k8s-openshell-tls-secret NAMESPACE=<ns>` | Create/update `openshell-tls` K8s secret in target namespace from `auth/openshell/` |
| `make openshell-status` | Show Helm release, gateway pods, CRDs, and local cert files |
| `make openshell-delete` | Uninstall OpenShell and delete the namespace |
| `make openshift-deploy` | Deploy Swarmer to OpenShift (uses `k8s/openshift/deployment.yaml`) |
| `make k8s-deploy` | Deploy Swarmer to a generic K8s/kind cluster |

## Workflow: fresh OpenShift install

1. **Verify context**
   ```sh
   kubectl config current-context
   ```
   Must point at the target OpenShift cluster, not a local kind cluster.

2. **Install OpenShell**
   ```sh
   make openshell-setup
   ```
   This installs CRDs + Helm chart, waits for gateway readiness, extracts certs
   to `auth/openshell/`, and attempts to create the `openshell-tls` secret in
   `$(NAMESPACE)` (default: `swarmer`).

3. **Fix OpenShift SCC (required on OpenShift)**

   OpenShell runs as UID 1000 which violates OpenShift's restricted SCC. Grant
   `anyuid` to its service accounts:
   ```sh
   oc adm policy add-scc-to-user anyuid -z openshell -n openshell
   oc adm policy add-scc-to-user anyuid -z openshell-sandbox -n openshell
   ```
   Then verify the StatefulSet rolls out:
   ```sh
   kubectl rollout status statefulset/openshell -n openshell --timeout=120s
   ```

4. **Validate certs match the cluster**

   Always verify the extracted certs are from the current cluster, not a stale
   dev environment:
   ```sh
   # Cluster fingerprint
   kubectl get secret openshell-client-tls -n openshell \
     -o jsonpath='{.data.ca\.crt}' | base64 -d | openssl x509 -noout -fingerprint

   # Local file fingerprint
   openssl x509 -noout -fingerprint -in auth/openshell/ca.crt
   ```
   If fingerprints differ, re-extract:
   ```sh
   make openshell-extract-tls
   make k8s-openshell-tls-secret NAMESPACE=swarmer
   ```

5. **Update `k8s/openshift/deployment.yaml`**

   The deployment needs OpenShell env vars and the TLS secret mounted. Add
   under `env:`:
   ```yaml
   - name: OPENSHELL_GATEWAY_URL
     value: "openshell.openshell.svc.cluster.local:8080"
   - name: OPENSHELL_TLS_CERT
     value: "/auth/openshell/tls.crt"
   - name: OPENSHELL_TLS_KEY
     value: "/auth/openshell/tls.key"
   - name: OPENSHELL_TLS_CA
     value: "/auth/openshell/ca.crt"
   ```
   Add under `volumeMounts:`:
   ```yaml
   - name: openshell-tls
     mountPath: /auth/openshell
     readOnly: true
   ```
   Add under `volumes:`:
   ```yaml
   - name: openshell-tls
     secret:
       secretName: openshell-tls
   ```

   For local kind (`k8s/swarmer/deployment.yaml`), apply the same changes but
   use `localhost:17670` for `OPENSHELL_GATEWAY_URL` (port-forward required).

6. **Deploy Swarmer**
   ```sh
   make openshift-deploy SILENT=1   # OpenShift
   # or
   make k8s-deploy                  # kind / generic K8s
   ```

7. **Verify**
   ```sh
   make openshell-status
   kubectl rollout status deployment/swarmer -n swarmer
   ```

## Workflow: cert rotation / stale certs

If `auth/openshell/` certs are from a different cluster or old deploy:

```sh
make openshell-extract-tls                        # re-pull from cluster
make k8s-openshell-tls-secret NAMESPACE=swarmer   # update secret
kubectl rollout restart deployment/swarmer -n swarmer
kubectl rollout status deployment/swarmer -n swarmer --timeout=120s
```

Always fingerprint-check before and after (see step 4 above).

## Workflow: local kind cluster

For local development, the gateway is not reachable in-cluster — use a
port-forward instead:

```sh
# Terminal 1 — keep running
kubectl port-forward -n openshell svc/openshell 17670:8080

# .env values for local dev
OPENSHELL_GATEWAY_URL=localhost:17670
OPENSHELL_TLS_CERT=auth/openshell/tls.crt
OPENSHELL_TLS_KEY=auth/openshell/tls.key
OPENSHELL_TLS_CA=auth/openshell/ca.crt
OPENSHELL_BEARER_TOKEN=
```

Run `python3 scripts/openshell_smoke_test.py` to verify before testing through
the UI.

## Common failures on OpenShift

| Symptom | Cause | Fix |
|---|---|---|
| `pods "openshell-0" is forbidden: unable to validate against any security context constraint` | SCC mismatch — UID 1000 not allowed | `oc adm policy add-scc-to-user anyuid -z openshell -n openshell` |
| Helm status `pending-install` with no pods | SCC blocked StatefulSet creation | Fix SCC, then verify with `kubectl rollout status statefulset/openshell -n openshell` |
| gRPC `certificate signed by unknown authority` | Stale certs from previous cluster | Re-extract and fingerprint-check (step 4) |
| `openshell-tls` secret not found in `swarmer` | Secret not created in app namespace | `make k8s-openshell-tls-secret NAMESPACE=swarmer` |
| Swarmer pod crashloops after cert update | Old secret still mounted | `kubectl rollout restart deployment/swarmer -n swarmer` |
