# ──────────────────────────────────────────────────────────────
#  Swarmer — Makefile
# ──────────────────────────────────────────────────────────────
#  Variables (override on the command line or in .env)
# ──────────────────────────────────────────────────────────────

# Container image settings
IMAGE        ?= swarmer
IMAGE_TAG    ?= latest
REGISTRY     ?=
# If REGISTRY is set, full ref is REGISTRY/IMAGE:TAG, otherwise IMAGE:TAG
IMAGE_REF     = $(if $(REGISTRY),$(REGISTRY)/$(IMAGE):$(IMAGE_TAG),$(IMAGE):$(IMAGE_TAG))

# docker or podman
CONTAINER_CMD ?= podman

# opencode agent image loaded into kind for session pods
OPENCODE_IMAGE ?= opencode-golang:latest

# Kubernetes
NAMESPACE    ?= swarmer
KIND_CLUSTER ?= swarmer

# Auth hash file (written by setup-auth)
AUTH_HASH_FILE ?= auth/password.hash

# ──────────────────────────────────────────────────────────────
#  Phony targets
# ──────────────────────────────────────────────────────────────
.PHONY: setup-auth install dev lint db-reset \
        image-build image-push \
        k8s-deploy k8s-auth-secret k8s-delete k8s-connect \
        kind-create kind-load kind-load-opencode kind-deploy kind-delete kind-connect \
        help

# ──────────────────────────────────────────────────────────────
#  Developer tooling
# ──────────────────────────────────────────────────────────────

setup-auth:  ## Prompt for a password and write argon2 hash to auth/password.hash
	@python3 scripts/setup_auth.py

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

image-build:  ## Build the swarmer container image  (IMAGE, IMAGE_TAG, REGISTRY)
	$(CONTAINER_CMD) build -f Containerfile -t $(IMAGE_REF) .
	@echo "Built: $(IMAGE_REF)"

image-push:  ## Push image to registry  (requires REGISTRY=...)
	@test -n "$(REGISTRY)" || (echo "Set REGISTRY=your.registry.example.com" && exit 1)
	$(CONTAINER_CMD) push $(IMAGE_REF)

# ──────────────────────────────────────────────────────────────
#  Deploy to an existing Kubernetes cluster
# ──────────────────────────────────────────────────────────────

k8s-auth-secret:  ## Create / update the swarmer-auth K8s Secret from auth/password.hash
	@test -f $(AUTH_HASH_FILE) || (echo "Run 'make setup-auth' first." && exit 1)
	kubectl create secret generic swarmer-auth \
	  --from-file=password.hash=$(AUTH_HASH_FILE) \
	  --namespace $(NAMESPACE) \
	  --dry-run=client -o yaml \
	  | kubectl apply -f -
	@echo "swarmer-auth secret updated in namespace $(NAMESPACE)."

k8s-deploy:  ## Deploy swarmer to the current kubectl context  (IMAGE_REF, NAMESPACE)
	@test -f $(AUTH_HASH_FILE) || (echo "Run 'make setup-auth' first." && exit 1)
	@echo "Deploying $(IMAGE_REF) → namespace $(NAMESPACE)..."
	# 1. Namespace + RBAC + PVC (order-independent, use || true for idempotency)
	kubectl apply -f k8s/swarmer/namespace.yaml
	kubectl apply -f k8s/swarmer/rbac.yaml
	kubectl apply -f k8s/swarmer/pvc.yaml
	kubectl apply -f k8s/swarmer/service.yaml
	# 2. Auth secret (create or update from local hash file)
	$(MAKE) k8s-auth-secret NAMESPACE=$(NAMESPACE)
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
	kubectl delete secret swarmer-auth -n $(NAMESPACE) --ignore-not-found
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

kind-deploy:  ## Create kind cluster + build image + deploy swarmer  (one-shot local dev)
	@test -f $(AUTH_HASH_FILE) || (echo "Run 'make setup-auth' first." && exit 1)
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
