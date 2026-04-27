# Plan: k8s-deploy missing AGENT_IMAGE env vars
**Date:** 2026-04-27
**Jira:** [ACM-33310](https://redhat.atlassian.net/browse/ACM-33310)

## Context
Running `make k8s-deploy` on a plain Kubernetes cluster produced a Swarmer deployment where `AGENT_IMAGE_OPENCODE` and `AGENT_IMAGE_CRUSH` were absent from the container environment. The "Image Found" indicator in the UI silently showed ❌ for all agent tools with no log output — because `get_image_available()` in `swarmer/k8s.py` short-circuits with `return False` when the image string is empty, before making any network call or emitting any log message.

The OpenShift deploy path (`make openshift-deploy` / `k8s/openshift/deployment.yaml`) had both env var declarations and Makefile `sed` substitutions correct. The plain k8s path had fallen behind on both counts.

## Approach
Fix both gaps in the k8s (non-OpenShift) path:
1. Add `AGENT_IMAGE_OPENCODE: AGENT_IMAGE_OPENCODE_VALUE` and `AGENT_IMAGE_CRUSH: AGENT_IMAGE_CRUSH_VALUE` placeholder env vars to `k8s/swarmer/deployment.yaml`, mirroring what `k8s/openshift/deployment.yaml` already had.
2. Add the corresponding `sed` substitution rules to the `k8s-deploy` Makefile target so the placeholders are replaced at deploy time with `$(AGENT_IMAGE_OPENCODE)` and `$(AGENT_IMAGE_CRUSH)`.

## Files to Change
- `k8s/swarmer/deployment.yaml` — add two env var entries with placeholder values
- `Makefile` (`k8s-deploy` target) — add two `sed` substitution expressions

## Verification
```bash
make k8s-deploy AGENT_IMAGE_OPENCODE=quay.io/jpacker/opencode:0.1.1
kubectl exec -n swarmer deployment/swarmer -- env | grep AGENT_IMAGE
# Expected: both vars present with correct image refs
# Then open the Swarmer UI → session detail → "Image Found:" should show ✅
```

---
## Implementation Summary
**Completed:** 2026-04-27

### What Changed
- `k8s/swarmer/deployment.yaml` — added `AGENT_IMAGE_OPENCODE: AGENT_IMAGE_OPENCODE_VALUE` and `AGENT_IMAGE_CRUSH: AGENT_IMAGE_CRUSH_VALUE` env var entries to the container spec, matching what `k8s/openshift/deployment.yaml` already had
- `Makefile` (`k8s-deploy` target) — added `s|AGENT_IMAGE_OPENCODE_VALUE|$(AGENT_IMAGE_OPENCODE)|g` and `s|AGENT_IMAGE_CRUSH_VALUE|$(AGENT_IMAGE_CRUSH)|g` to the `sed` substitution block so the placeholders are replaced at deploy time

### Tests
- No automated tests for Makefile/manifest rendering; verify manually with:
  ```bash
  make k8s-deploy AGENT_IMAGE_OPENCODE=quay.io/jpacker/opencode:0.1.1
  kubectl exec -n swarmer deployment/swarmer -- env | grep AGENT_IMAGE
  ```

### Known Gaps / Follow-up
- No automated test for manifest placeholder substitution — a `make dry-run` or manifest-render target would catch this class of regression
- `AGENT_IMAGE_OPENCODE` defaults in the Makefile (`quay.io/jpacker/opencode:0.1.1`) may drift from the actual published image; `make sync-images` should be run to keep them aligned with `../agent-containers/.push-defaults`
