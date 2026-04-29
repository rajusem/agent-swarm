# Fix: Image Reachability Check Always Returning False

**Date:** 2026-04-15
**PR:** [#10 fix: use registry probe for tool image availability](https://github.com/stolostron/agent-swarm/pull/10)

## Problem

The ✓/✗ image reachability indicators on the new session and detail pages always showed ✗ even when the images (`quay.io/jpacker/opencode-golang:latest`, `quay.io/jpacker/opencode-python:latest`) were confirmed to exist in the registry.

## Investigation

From `.env`:
- `AGENT_IMAGE=quay.io/jpacker/opencode-golang:latest`
- Python image derived as `quay.io/jpacker/opencode-python:latest`

The manifest URL being checked: `https://quay.io/v2/jpacker/opencode-golang/manifests/latest`

Two likely causes:

1. **Silent exception swallowing** — the original `check_image_reachable` had broad `except Exception: pass` blocks that hid all errors, making it impossible to diagnose failures.

2. **Missing `Accept` header** — the Docker registry v2 spec requires clients to send `Accept` headers listing supported manifest media types. Without them, registries like quay.io may return a non-200 response even for valid images.

## Implementation

**File:** `swarmer/k8s.py` — `check_image_reachable()`

### 1. Added `Accept` header to manifest requests

```python
accept = (
    "application/vnd.docker.distribution.manifest.v2+json,"
    "application/vnd.oci.image.manifest.v1+json,"
    "application/vnd.oci.image.index.v1+json,"
    "*/*"
)
headers: dict[str, str] = {"Accept": accept}
```

Covers Docker v2 manifests, OCI image manifests, and OCI image indexes (multi-arch).

### 2. Replaced silent exception blocks with structured logging

All `except Exception: pass` blocks replaced with `log.warning` / `log.debug` calls:

- Pull secret read failure: `log.warning("could not read pull secret %s/%s: %s", ...)`
- Non-200/401 response: `log.warning("unhandled response %s for %s", ...)`
- HTTP/network error: `log.warning("HTTP error for %s: %s", ...)`
- Key intermediate values logged at DEBUG: image parsed values, auth presence, status codes at each step of the Bearer flow

## Verification

Restart the server and load the New Session page. Either:
- Images now show ✓ (Accept header was the fix)
- Server logs show a specific `check_image_reachable: WARNING` line identifying the remaining failure point (pull secret read, registry auth, or network)
