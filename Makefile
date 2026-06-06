# ──────────────────────────────────────────────────────────────
#  Swarmer — Makefile
# ──────────────────────────────────────────────────────────────
#  Variables (override on the command line or in .env)
# ──────────────────────────────────────────────────────────────
-include .env

# Container image settings
IMAGE        ?= swarmer
IMAGE_TAG    ?= $(shell cat VERSION)
REGISTRY     ?=
# If REGISTRY is set, full ref is REGISTRY/IMAGE:TAG, otherwise IMAGE:TAG
IMAGE_REF     = $(if $(REGISTRY),$(REGISTRY)/$(IMAGE):$(IMAGE_TAG),$(IMAGE):$(IMAGE_TAG))

# docker or podman
CONTAINER_CMD ?= podman

# opencode agent image loaded into kind for session pods
OPENCODE_IMAGE ?= quay.io/jpacker/opencode:0.1.1

# Agent tool images (overridable via .env or command line)
AGENT_IMAGE_OPENCODE ?= quay.io/jpacker/opencode:0.1.1
AGENT_IMAGE_CRUSH    ?= ghcr.io/gurnben/crush-container:latest

# Crush agent image
CRUSH_IMAGE   ?= crush:latest
CRUSH_VERSION ?= 0.1.127

# Kubernetes
NAMESPACE            ?= swarmer
KIND_CLUSTER         ?= swarmer
OPENSHIFT_OAUTH_URL  ?=
SWARMER_HOST         ?=

# OpenShell gateway
OPENSHELL_VERSION   ?= 0.0.55
OPENSHELL_NAMESPACE ?= openshell
OPENSHELL_TLS_DIR   ?= auth/openshell

# agent-containers build defaults (registry + image tag — checked in)
AC_DEFAULTS ?= .push-defaults

# ──────────────────────────────────────────────────────────────
#  Phony targets
# ──────────────────────────────────────────────────────────────
.PHONY: setup-secret k8s-secret k8s-openshell-tls-secret user-token grant-workspace \
        install dev lint test db-reset \
        image-build image-push image-build-crush \
        k8s-deploy k8s-delete k8s-connect \
        openshift-deploy \
        kind-create kind-load kind-load-opencode kind-load-crush kind-deploy kind-delete kind-connect \
        openshell-setup openshell-extract-tls openshell-gen-token openshell-status openshell-delete \
        sync-images help

# ──────────────────────────────────────────────────────────────
#  Developer tooling
# ──────────────────────────────────────────────────────────────

sync-images:  ## Sync AGENT_IMAGE / AGENT_IMAGE_OPENCODE / AGENT_IMAGE_CRUSH in .env from .push-defaults
	@test -f $(AC_DEFAULTS) || (echo "$(AC_DEFAULTS) not found — create/update .push-defaults first" && exit 1)
	$(eval AC_REGISTRY := $(shell grep '^REGISTRY=' $(AC_DEFAULTS) | cut -d= -f2-))
	$(eval AC_TAG      := $(shell grep '^IMAGE_TAG=' $(AC_DEFAULTS) | cut -d= -f2-))
	@echo "Syncing agent images → $(AC_REGISTRY)/{opencode,crush}:$(AC_TAG)"
	@sed -i "s|^AGENT_IMAGE=.*|AGENT_IMAGE=$(AC_REGISTRY)/opencode:$(AC_TAG)|" .env
	@sed -i "s|^AGENT_IMAGE_OPENCODE=.*|AGENT_IMAGE_OPENCODE=$(AC_REGISTRY)/opencode:$(AC_TAG)|" .env
	@sed -i "s|^AGENT_IMAGE_CRUSH=.*|AGENT_IMAGE_CRUSH=$(AC_REGISTRY)/crush:$(AC_TAG)|" .env
	@echo "Updated .env"

setup-secret:  ## Generate a new SWARMER_SECRET_KEY and save to auth/secret.key
	@mkdir -p auth
	@python3 -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())" > auth/secret.key
	@echo "Secret key written to auth/secret.key"

k8s-secret:  ## Create/update the swarmer-secret K8s Secret from auth/secret.key
	@test -f auth/secret.key || (echo "Run 'make setup-secret' first" && exit 1)
	kubectl create secret generic swarmer-secret \
	  --from-literal=SWARMER_SECRET_KEY=$$(cat auth/secret.key) \
	  -n $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -

k8s-openshell-tls-secret:  ## Create/update the openshell-tls K8s Secret from auth/openshell/ certs
	@test -f $(OPENSHELL_TLS_DIR)/ca.crt || (echo "OpenShell mTLS certs not found. Run 'make openshell-setup' first." && exit 1)
	kubectl create secret generic openshell-tls \
	  --from-file=ca.crt=$(OPENSHELL_TLS_DIR)/ca.crt \
	  --from-file=tls.crt=$(OPENSHELL_TLS_DIR)/tls.crt \
	  --from-file=tls.key=$(OPENSHELL_TLS_DIR)/tls.key \
	  -n $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -

TOKEN_DURATION ?= 8h

user-token:  ## Issue a login token for a K3s/Kind user  (SA_USER=alice, TOKEN_DURATION=8h)
	@test -n "$(SA_USER)" || (echo "Usage: make user-token SA_USER=<name>" && exit 1)
	@kubectl create serviceaccount $(SA_USER) -n $(NAMESPACE) \
	  --dry-run=client -o yaml | kubectl apply -f - > /dev/null
	@echo ""
	@echo "Token for '$(SA_USER)' (valid $(TOKEN_DURATION)):"
	@echo "──────────────────────────────────────────────────"
	@kubectl create token $(SA_USER) -n $(NAMESPACE) --duration=$(TOKEN_DURATION)
	@echo "──────────────────────────────────────────────────"
	@echo "Paste this token into the Swarmer login page."
	@echo "Grant workspace access with: make grant-workspace SA_USER=$(SA_USER) WORKSPACE_NS=<ns>"

grant-workspace:  ## Grant a user access to a workspace namespace  (SA_USER=alice, WORKSPACE_NS=my-project)
	@test -n "$(SA_USER)"      || (echo "Usage: make grant-workspace SA_USER=<name> WORKSPACE_NS=<ns>" && exit 1)
	@test -n "$(WORKSPACE_NS)" || (echo "Usage: make grant-workspace SA_USER=<name> WORKSPACE_NS=<ns>" && exit 1)
	kubectl create rolebinding swarmer-user-$(SA_USER) \
	  --clusterrole=swarmer-user \
	  --serviceaccount=$(NAMESPACE):$(SA_USER) \
	  --namespace=$(WORKSPACE_NS) \
	  --dry-run=client -o yaml | kubectl apply -f -
	@echo "$(SA_USER) can now access workspace namespace '$(WORKSPACE_NS)'."

install:  ## Install Python dependencies
	pip install -r requirements.txt

dev:  ## Run development server with auto-reload (uses local kubeconfig)
	@echo ""
	@echo "╔══════════════════════════════════════════════════════╗"
	@echo "║  Swarmer dev server → http://localhost:8090          ║"
	@echo "╚══════════════════════════════════════════════════════╝"
	@echo ""
	K8S_IN_CLUSTER=false uvicorn swarmer.main:app --host 0.0.0.0 --port 8090 --reload

lint:  ## Run ruff linter
	ruff check swarmer/

test:  ## Run unit tests (excludes Playwright browser tests)
	python3 -m pytest tests/ -q --ignore=tests/test_ui_patternfly.py
	python3 -m pip install -q -e "mcp-server[dev]"
	python3 -m pytest mcp-server/tests/ -q --rootdir=mcp-server

db-reset:  ## Delete the SQLite database (forces fresh schema on next start)
	@rm -f data/swarmer.db && echo "Database deleted."

# ──────────────────────────────────────────────────────────────
#  Container image
# ──────────────────────────────────────────────────────────────

image-build: sync-images  ## Build the swarmer container image  (REGISTRY, SILENT=1 to skip version prompt)
	@set -e; \
	CURRENT=$$(cat VERSION); \
	if [ "$(SILENT)" != "1" ]; then \
		printf "Image version [$$CURRENT]: "; \
		read INPUT; \
		if [ -n "$$INPUT" ]; then \
			printf "$$INPUT\n" > VERSION; \
			TAG=$$INPUT; \
		else \
			TAG=$$CURRENT; \
		fi; \
	else \
		TAG=$$CURRENT; \
	fi; \
	IMAGE_REF="$(if $(REGISTRY),$(REGISTRY)/$(IMAGE),$(IMAGE)):$$TAG"; \
	echo "Building $$IMAGE_REF..."; \
	$(CONTAINER_CMD) build -f Containerfile -t "$$IMAGE_REF" .; \
	echo "Built: $$IMAGE_REF"

image-push:  ## Push image to registry  (requires REGISTRY=..., uses VERSION file)
	@test -n "$(REGISTRY)" || (echo "Set REGISTRY=your.registry.example.com" && exit 1)
	@TAG=$$(cat VERSION); \
	IMAGE_REF="$(REGISTRY)/$(IMAGE):$$TAG"; \
	echo "Pushing $$IMAGE_REF..."; \
	$(CONTAINER_CMD) push "$$IMAGE_REF"; \
	echo "Pushed: $$IMAGE_REF"

# ──────────────────────────────────────────────────────────────
#  Deploy to an existing Kubernetes cluster
# ──────────────────────────────────────────────────────────────

k8s-deploy:  ## Deploy swarmer to the current kubectl context  (IMAGE_REF, NAMESPACE)
	@test -f auth/secret.key || (echo "Run 'make setup-secret' first." && exit 1)
	@echo "Deploying $(IMAGE_REF) → namespace $(NAMESPACE)..."
	# 1. Namespace + RBAC + PVC (order-independent, use || true for idempotency)
	kubectl apply -f k8s/swarmer/namespace.yaml
	kubectl apply -f k8s/swarmer/rbac.yaml
	kubectl apply -f k8s/swarmer/pvc.yaml
	kubectl apply -f k8s/swarmer/service.yaml
	# 2. Secret key (create or update from local key file)
	$(MAKE) k8s-secret NAMESPACE=$(NAMESPACE)
	# 3. Deployment — substitute image + OpenShift OAuth URL placeholders then apply
	@OAUTH_URL="$(OPENSHIFT_OAUTH_URL)"; \
	if [ -z "$$OAUTH_URL" ]; then \
	  DETECTED=$$(kubectl get route oauth-openshift -n openshift-authentication \
	    -o jsonpath='{.spec.host}' 2>/dev/null); \
	  if [ -n "$$DETECTED" ]; then \
	    OAUTH_URL="https://$$DETECTED"; \
	    echo "Auto-detected OpenShift OAuth URL: $$OAUTH_URL"; \
	  else \
	    printf "OPENSHIFT_OAUTH_URL (leave blank for token-paste-only login): "; \
	    read OAUTH_URL; \
	  fi; \
	fi; \
	PREV_MAX=$$(grep '^MAX_CONCURRENT_AGENTS=' .deploy-defaults 2>/dev/null | cut -d= -f2); \
	DEF_MAX=$${PREV_MAX:-5}; \
	MAX_VAL="$(MAX_CONCURRENT_AGENTS)"; \
	if [ -z "$$MAX_VAL" ] && [ "$(SILENT)" != "1" ]; then \
	  printf "MAX_CONCURRENT_AGENTS [$$DEF_MAX]: "; \
	  read MAX_INPUT; \
	  MAX_VAL=$${MAX_INPUT:-$$DEF_MAX}; \
	else \
	  MAX_VAL=$${MAX_VAL:-$$DEF_MAX}; \
	fi; \
	grep -v '^MAX_CONCURRENT_AGENTS=' .deploy-defaults 2>/dev/null > .deploy-defaults.tmp || true; \
	echo "MAX_CONCURRENT_AGENTS=$$MAX_VAL" >> .deploy-defaults.tmp; \
	mv .deploy-defaults.tmp .deploy-defaults; \
	sed "s|SWARMER_IMAGE|$(IMAGE_REF)|g; \
	     s|OPENSHIFT_OAUTH_URL_VALUE|$$OAUTH_URL|g; \
	     s|REDIRECT_BASE_URL_VALUE||g; \
	     s|AGENT_IMAGE_OPENCODE_VALUE|$(AGENT_IMAGE_OPENCODE)|g; \
	     s|AGENT_IMAGE_CRUSH_VALUE|$(AGENT_IMAGE_CRUSH)|g; \
	     s|MAX_CONCURRENT_AGENTS_VALUE|$$MAX_VAL|g" \
	  k8s/swarmer/deployment.yaml | kubectl apply -f -
	# 4. Wait for rollout
	kubectl rollout status deployment/swarmer -n $(NAMESPACE) --timeout=120s
	@echo ""
	@echo "✓ Swarmer deployed."
	@ROUTE=$$(kubectl get route swarmer -n $(NAMESPACE) -o jsonpath='{.spec.host}' 2>/dev/null); \
	if [ -n "$$ROUTE" ]; then \
	  echo "  Dashboard → https://$$ROUTE"; \
	else \
	  NODE_IP=$$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="ExternalIP")].address}' 2>/dev/null); \
	  if [ -z "$$NODE_IP" ]; then \
	    NODE_IP=$$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null); \
	  fi; \
	  if [ -n "$$NODE_IP" ]; then \
	    echo "  Dashboard → http://$$NODE_IP:30080"; \
	  else \
	    echo "  Run 'make k8s-connect' to open the dashboard (port-forward to localhost:8080)."; \
	  fi; \
	fi

k8s-connect:  ## Port-forward the swarmer dashboard to localhost:8080
	@echo "Forwarding http://localhost:8080 → swarmer service..."
	kubectl port-forward -n $(NAMESPACE) service/swarmer 8080:8080

openshift-deploy:  ## Deploy to OpenShift: Route + OAuthClient + app  (SWARMER_HOST=optional)
	@test -f auth/secret.key || (echo "Run 'make setup-secret' first." && exit 1)
	@echo "Deploying to OpenShift namespace $(NAMESPACE)..."
	kubectl apply -f k8s/swarmer/namespace.yaml
	kubectl apply -f k8s/swarmer/rbac.yaml
	kubectl apply -f k8s/swarmer/pvc.yaml
	kubectl apply -f k8s/openshift/service.yaml
	kubectl apply -f k8s/openshift/route.yaml
	@if [ -n "$(SWARMER_HOST)" ]; then \
	  kubectl patch route swarmer -n $(NAMESPACE) --type=merge \
	    -p "{\"spec\":{\"host\":\"$(SWARMER_HOST)\"}}"; \
	fi
	$(MAKE) k8s-secret NAMESPACE=$(NAMESPACE)
	@echo "Waiting for Route hostname..."
	@ROUTE_HOST="$(SWARMER_HOST)"; \
	if [ -z "$$ROUTE_HOST" ]; then \
	  for i in $$(seq 1 15); do \
	    ROUTE_HOST=$$(kubectl get route swarmer -n $(NAMESPACE) \
	      -o jsonpath='{.spec.host}' 2>/dev/null); \
	    [ -n "$$ROUTE_HOST" ] && break; \
	    sleep 2; \
	  done; \
	fi; \
	if [ -z "$$ROUTE_HOST" ]; then \
	  echo "Error: Route hostname not assigned after 30s."; \
	  echo "       Check: kubectl get route swarmer -n $(NAMESPACE)"; \
	  exit 1; \
	fi; \
	echo "Route: https://$$ROUTE_HOST"; \
	sed "s|SWARMER_HOST|$$ROUTE_HOST|g" k8s/openshift/oauth-client.yaml | kubectl apply -f -; \
	echo "OAuthClient registered → https://$$ROUTE_HOST/auth/callback"; \
	OAUTH_HOST=$$(kubectl get route oauth-openshift -n openshift-authentication \
	  -o jsonpath='{.spec.host}' 2>/dev/null); \
	if [ -n "$$OAUTH_HOST" ]; then \
	  OAUTH_URL="https://$$OAUTH_HOST"; \
	  echo "Auto-detected OpenShift OAuth URL: $$OAUTH_URL"; \
	else \
	  printf "OPENSHIFT_OAUTH_URL (e.g. https://oauth-openshift.apps.example.com): "; \
	  read OAUTH_URL; \
	fi; \
	PREV_MAX=$$(grep '^MAX_CONCURRENT_AGENTS=' .deploy-defaults 2>/dev/null | cut -d= -f2); \
	DEF_MAX=$${PREV_MAX:-5}; \
	MAX_VAL="$(MAX_CONCURRENT_AGENTS)"; \
	if [ -z "$$MAX_VAL" ] && [ "$(SILENT)" != "1" ]; then \
	  printf "MAX_CONCURRENT_AGENTS [$$DEF_MAX]: "; \
	  read MAX_INPUT; \
	  MAX_VAL=$${MAX_INPUT:-$$DEF_MAX}; \
	else \
	  MAX_VAL=$${MAX_VAL:-$$DEF_MAX}; \
	fi; \
	grep -v '^MAX_CONCURRENT_AGENTS=' .deploy-defaults 2>/dev/null > .deploy-defaults.tmp || true; \
	echo "MAX_CONCURRENT_AGENTS=$$MAX_VAL" >> .deploy-defaults.tmp; \
	mv .deploy-defaults.tmp .deploy-defaults; \
	sed "s|SWARMER_IMAGE|$(IMAGE_REF)|g; \
	     s|OPENSHIFT_OAUTH_URL_VALUE|$$OAUTH_URL|g; \
	     s|AGENT_IMAGE_OPENCODE_VALUE|$(AGENT_IMAGE_OPENCODE)|g; \
	     s|AGENT_IMAGE_CRUSH_VALUE|$(AGENT_IMAGE_CRUSH)|g; \
	     s|MAX_CONCURRENT_AGENTS_VALUE|$$MAX_VAL|g" \
	  k8s/openshift/deployment.yaml | kubectl apply -f -
	kubectl rollout status deployment/swarmer -n $(NAMESPACE) --timeout=120s
	@echo ""
	@echo "✓ OpenShift deployment complete."
	@ROUTE=$$(kubectl get route swarmer -n $(NAMESPACE) \
	  -o jsonpath='{.spec.host}' 2>/dev/null); \
	[ -n "$$ROUTE" ] && echo "  Dashboard → https://$$ROUTE" || true

k8s-delete:  ## Remove swarmer from Kubernetes (keeps the kind cluster if any)
	@echo "Removing swarmer from namespace $(NAMESPACE)..."
	kubectl delete -f k8s/swarmer/deployment.yaml --ignore-not-found
	kubectl delete -f k8s/swarmer/service.yaml --ignore-not-found
	kubectl delete -f k8s/swarmer/pvc.yaml --ignore-not-found
	kubectl delete secret swarmer-secret -n $(NAMESPACE) --ignore-not-found
	kubectl delete -f k8s/swarmer/rbac.yaml --ignore-not-found
	kubectl delete -f k8s/swarmer/namespace.yaml --ignore-not-found
	@echo "✓ Swarmer removed."

# ──────────────────────────────────────────────────────────────
#  kind (local development cluster)
# ──────────────────────────────────────────────────────────────

kind-create:  ## Create a kind cluster named '$(KIND_CLUSTER)' with host port 8080→30080
	@if kind get clusters 2>/dev/null | grep -q "^$(KIND_CLUSTER)$$"; then \
	  echo "kind cluster '$(KIND_CLUSTER)' already exists — skipping creation."; \
	else \
	  kind create cluster --name $(KIND_CLUSTER) --config k8s/kind-config.yaml; \
	  echo "✓ kind cluster '$(KIND_CLUSTER)' created."; \
	fi

kind-load:  ## Load the swarmer image into the kind cluster (no registry needed)
	@echo "Loading $(IMAGE_REF) into kind cluster '$(KIND_CLUSTER)'..."
	@if [ "$(CONTAINER_CMD)" = "podman" ]; then \
	  podman save $(IMAGE_REF) | kind load image-archive /dev/stdin --name $(KIND_CLUSTER); \
	else \
	  kind load docker-image $(IMAGE_REF) --name $(KIND_CLUSTER); \
	fi
	@echo "✓ Image loaded."

kind-load-opencode:  ## Load the opencode image into the kind cluster  (OPENCODE_IMAGE)
	@echo "Tagging $(OPENCODE_IMAGE) → opencode:latest"
	$(CONTAINER_CMD) tag $(OPENCODE_IMAGE) opencode:latest
	@echo "Loading opencode:latest into kind cluster '$(KIND_CLUSTER)'..."
	@if [ "$(CONTAINER_CMD)" = "podman" ]; then \
	  podman save opencode:latest | kind load image-archive /dev/stdin --name $(KIND_CLUSTER); \
	else \
	  kind load docker-image opencode:latest --name $(KIND_CLUSTER); \
	fi
	@echo "✓ opencode image loaded as opencode:latest"

image-build-crush:  ## Build the Crush agent container image
	$(CONTAINER_CMD) build -f Containerfile.crush \
	  --build-arg CRUSH_VERSION=$(CRUSH_VERSION) \
	  -t $(CRUSH_IMAGE) .
	@echo "Built: $(CRUSH_IMAGE)"

kind-load-crush:  ## Load the Crush agent image into kind
	@echo "Loading $(CRUSH_IMAGE) into kind cluster '$(KIND_CLUSTER)'..."
	@if [ "$(CONTAINER_CMD)" = "podman" ]; then \
	  podman save $(CRUSH_IMAGE) | kind load image-archive /dev/stdin --name $(KIND_CLUSTER); \
	else \
	  kind load docker-image $(CRUSH_IMAGE) --name $(KIND_CLUSTER); \
	fi
	@echo "✓ Crush image loaded."

kind-deploy:  ## Create kind cluster + build image + deploy swarmer  (one-shot local dev)
	@test -f auth/secret.key || (echo "Run 'make setup-secret' first." && exit 1)
	$(MAKE) kind-create
	$(MAKE) image-build
	$(MAKE) kind-load
	$(MAKE) kind-load-opencode
	$(MAKE) k8s-deploy
	@echo ""
	@echo "╔══════════════════════════════════════════════════════╗"
	@echo "║  Swarmer is running in kind!                         ║"
	@echo "║  Dashboard → http://localhost:8080                   ║"
	@echo "╚══════════════════════════════════════════════════════╝"

kind-connect:  ## Open a port-forward to the kind-deployed dashboard on localhost:8080
	$(MAKE) k8s-connect

kind-delete:  ## Delete the kind cluster (removes all data inside it)
	kind delete cluster --name $(KIND_CLUSTER)
	@echo "✓ kind cluster '$(KIND_CLUSTER)' deleted."

# ──────────────────────────────────────────────────────────────
#  OpenShell gateway (installs to the CURRENT kubectl context)
# ──────────────────────────────────────────────────────────────

openshell-setup:  ## Install OpenShell + Agent Sandbox CRDs on current kubectl context (idempotent)
	@HELM_VER=$$(helm version --short 2>/dev/null | grep -oP 'v\K[0-9]+\.[0-9]+' | head -1); \
	HELM_MAJOR=$$(echo "$$HELM_VER" | cut -d. -f1); \
	HELM_MINOR=$$(echo "$$HELM_VER" | cut -d. -f2); \
	if [ -z "$$HELM_VER" ] || { [ "$$HELM_MAJOR" -lt 4 ] && { [ "$$HELM_MAJOR" -lt 3 ] || [ "$$HELM_MINOR" -lt 8 ]; }; }; then \
	  echo "Error: Helm 3.8+ required for OCI chart support (found: $$(helm version --short 2>/dev/null || echo 'not installed'))"; \
	  exit 1; \
	fi
	@echo "Installing Agent Sandbox CRDs..."
	kubectl apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/latest/download/manifest.yaml
	@echo "Installing OpenShell $(OPENSHELL_VERSION)..."
	kubectl create namespace $(OPENSHELL_NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	DOCKER_CONFIG=$$(mktemp -d) helm upgrade --install openshell \
	  oci://ghcr.io/nvidia/openshell/helm-chart \
	  --version $(OPENSHELL_VERSION) \
	  --namespace $(OPENSHELL_NAMESPACE) \
	  --set server.auth.allowUnauthenticatedUsers=true \
	  --wait --timeout 5m
	$(MAKE) openshell-extract-tls
	$(MAKE) k8s-openshell-tls-secret NAMESPACE=$(NAMESPACE) 2>/dev/null || true
	@echo ""
	@echo "✓ OpenShell $(OPENSHELL_VERSION) installed."
	@echo "  Port-forward: kubectl port-forward -n $(OPENSHELL_NAMESPACE) svc/openshell 17670:8080"
	@echo "  TLS certs:    $(OPENSHELL_TLS_DIR)/"

openshell-extract-tls:  ## Extract mTLS client certs from cluster to auth/openshell/
	@mkdir -p $(OPENSHELL_TLS_DIR)
	kubectl -n $(OPENSHELL_NAMESPACE) get secret openshell-client-tls \
	  -o jsonpath='{.data.ca\.crt}'  | base64 -d > $(OPENSHELL_TLS_DIR)/ca.crt
	kubectl -n $(OPENSHELL_NAMESPACE) get secret openshell-client-tls \
	  -o jsonpath='{.data.tls\.crt}' | base64 -d > $(OPENSHELL_TLS_DIR)/tls.crt
	kubectl -n $(OPENSHELL_NAMESPACE) get secret openshell-client-tls \
	  -o jsonpath='{.data.tls\.key}' | base64 -d > $(OPENSHELL_TLS_DIR)/tls.key
	@echo "✓ mTLS certs written to $(OPENSHELL_TLS_DIR)/"

openshell-gen-token:  ## Generate a JWT bearer token for the in-cluster OIDC provider and append to .env
	@TOKEN=$$(python3 scripts/openshell_gen_token.py) && \
	sed -i '/^OPENSHELL_BEARER_TOKEN=/d' .env 2>/dev/null || true && \
	echo "OPENSHELL_BEARER_TOKEN=$$TOKEN" >> .env && \
	echo "✓ OPENSHELL_BEARER_TOKEN appended to .env (valid 30 days)"

openshell-status:  ## Show OpenShell installation status on current kubectl context
	@echo "=== Helm release ==="
	@helm status openshell -n $(OPENSHELL_NAMESPACE) 2>/dev/null || echo "  (not installed)"
	@echo ""
	@echo "=== Gateway pods ==="
	@kubectl get pods -n $(OPENSHELL_NAMESPACE) 2>/dev/null || echo "  (namespace not found)"
	@echo ""
	@echo "=== Agent Sandbox CRDs ==="
	@kubectl get crds | grep -i sandbox 2>/dev/null || echo "  (none)"
	@echo ""
	@echo "=== mTLS certs ==="
	@ls -la $(OPENSHELL_TLS_DIR)/ 2>/dev/null || echo "  (not extracted — run 'make openshell-extract-tls')"

openshell-delete:  ## Remove OpenShell and Agent Sandbox CRDs from current kubectl context
	@echo "Removing OpenShell..."
	helm uninstall openshell -n $(OPENSHELL_NAMESPACE) 2>/dev/null || true
	kubectl delete namespace $(OPENSHELL_NAMESPACE) --ignore-not-found
	@echo "✓ OpenShell removed."

# ──────────────────────────────────────────────────────────────
#  Help
# ──────────────────────────────────────────────────────────────

help:  ## Show this help
	@echo "Swarmer Makefile targets:"
	@echo ""
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' Makefile \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' \
	  | sort
	@echo ""
	@echo "Variables (override on CLI, e.g. make kind-deploy IMAGE_TAG=v1.2):"
	@echo "  IMAGE=$(IMAGE)  IMAGE_TAG=$(IMAGE_TAG)  REGISTRY=$(REGISTRY)"
	@echo "  CONTAINER_CMD=$(CONTAINER_CMD)  KIND_CLUSTER=$(KIND_CLUSTER)"

.DEFAULT_GOAL := help