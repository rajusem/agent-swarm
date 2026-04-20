# ──────────────────────────────────────────────────────────────
#  Swarmer — Makefile
# ──────────────────────────────────────────────────────────────
#  Variables (override on the command line or in .env)
# ──────────────────────────────────────────────────────────────

# Container image settings
IMAGE        ?= swarmer
IMAGE_TAG    ?= $(shell cat VERSION)
REGISTRY     ?=
# If REGISTRY is set, full ref is REGISTRY/IMAGE:TAG, otherwise IMAGE:TAG
IMAGE_REF     = $(if $(REGISTRY),$(REGISTRY)/$(IMAGE):$(IMAGE_TAG),$(IMAGE):$(IMAGE_TAG))

# docker or podman
CONTAINER_CMD ?= podman

# opencode agent image loaded into kind for session pods
OPENCODE_IMAGE ?= opencode-golang:latest

# Crush agent image
CRUSH_IMAGE   ?= crush:latest
CRUSH_VERSION ?= 0.1.127

# Kubernetes
NAMESPACE    ?= swarmer
KIND_CLUSTER ?= swarmer

# agent-containers build defaults (registry + image tag shared with sibling repo)
AC_DEFAULTS ?= ../agent-containers/.push-defaults

# ──────────────────────────────────────────────────────────────
#  Phony targets
# ──────────────────────────────────────────────────────────────
.PHONY: setup-secret k8s-secret user-token grant-workspace \
        install dev lint db-reset \
        image-build image-push image-build-crush \
        k8s-deploy k8s-delete k8s-connect \
        kind-create kind-load kind-load-opencode kind-load-crush kind-deploy kind-delete kind-connect \
        sync-images help

# ──────────────────────────────────────────────────────────────
#  Developer tooling
# ──────────────────────────────────────────────────────────────

sync-images:  ## Sync AGENT_IMAGE / AGENT_IMAGE_OPENCODE / AGENT_IMAGE_PYTHON in .env from ../agent-containers/.push-defaults
	@test -f $(AC_DEFAULTS) || (echo "$(AC_DEFAULTS) not found — run 'make publish' in ../agent-containers first" && exit 1)
	$(eval AC_REGISTRY := $(shell grep '^REGISTRY=' $(AC_DEFAULTS) | cut -d= -f2-))
	$(eval AC_TAG      := $(shell grep '^IMAGE_TAG=' $(AC_DEFAULTS) | cut -d= -f2-))
	@echo "Syncing agent images → $(AC_REGISTRY)/opencode-{golang,python}:$(AC_TAG)"
	@sed -i "s|^AGENT_IMAGE=.*|AGENT_IMAGE=$(AC_REGISTRY)/opencode-golang:$(AC_TAG)|" .env
	@sed -i "s|^AGENT_IMAGE_OPENCODE=.*|AGENT_IMAGE_OPENCODE=$(AC_REGISTRY)/opencode-golang:$(AC_TAG)|" .env
	@sed -i "s|^AGENT_IMAGE_PYTHON=.*|AGENT_IMAGE_PYTHON=$(AC_REGISTRY)/opencode-python:$(AC_TAG)|" .env
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
	# 3. Deployment — substitute SWARMER_IMAGE placeholder then apply
	sed "s|SWARMER_IMAGE|$(IMAGE_REF)|g" k8s/swarmer/deployment.yaml \
	  | kubectl apply -f -
	# 4. Wait for rollout
	kubectl rollout status deployment/swarmer -n $(NAMESPACE) --timeout=120s
	@echo ""
	@echo "✓ Swarmer deployed."
	@echo "  Run 'make k8s-connect' to open the dashboard."

k8s-connect:  ## Port-forward the swarmer dashboard to localhost:8080
	@echo "Forwarding http://localhost:8080 → swarmer service..."
	kubectl port-forward -n $(NAMESPACE) service/swarmer 8080:8080

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

kind-load-opencode:  ## Load the opencode-golang image into the kind cluster as 'latest'  (OPENCODE_IMAGE)
	@echo "Tagging $(OPENCODE_IMAGE) → opencode-golang:latest"
	$(CONTAINER_CMD) tag $(OPENCODE_IMAGE) opencode-golang:latest
	@echo "Loading opencode-golang:latest into kind cluster '$(KIND_CLUSTER)'..."
	@if [ "$(CONTAINER_CMD)" = "podman" ]; then \
	  podman save opencode-golang:latest | kind load image-archive /dev/stdin --name $(KIND_CLUSTER); \
	else \
	  kind load docker-image opencode-golang:latest --name $(KIND_CLUSTER); \
	fi
	@echo "✓ opencode image loaded as opencode-golang:latest"

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
