# Swarmer — Multi-Agent-Tool Architecture

**Date:** 2026-04-15
**Status:** Proposed
**Author:** Architecture Design (Crush-assisted)

## 1. Problem Statement

Swarmer is tightly coupled to OpenCode as the sole agent CLI tool. Every layer — from the database model (`OpencodeSecret`), to the K8s ConfigMap (`opencode-config`), to the pod spec builder (`opencode serve`, `opencode run`), to the UI templates — assumes OpenCode is the only tool that will ever run inside a session pod.

We need to support **Crush CLI** (from Charmbracelet) and make the architecture extensible enough that adding a third or fourth agent tool in the future is a configuration exercise, not a refactor.

---

## 2. Current Architecture: OpenCode Coupling Points

An audit of the codebase reveals **9 distinct coupling points** where OpenCode is hardwired:

| # | File | Coupling |
|---|------|----------|
| 1 | `config.py:12` | `agent_image: str = "opencode-golang:latest"` — single image for all sessions |
| 2 | `models/opencode_secret.py` | Model named `OpencodeSecret` with GCP/Vertex-specific fields only |
| 3 | `models/session.py:24` | Session modes `("tui", "server", "prompt")` reference opencode subcommands |
| 4 | `k8s.py:76-104` | `_build_opencode_config()` generates `opencode.json` content |
| 5 | `k8s.py:107-133` | `apply_opencode_config()` creates ConfigMap named `opencode-config` |
| 6 | `k8s.py:337-364` | `exec_model_json()` writes opencode-specific model state file |
| 7 | `k8s_session.py:63-356` | `build_session_pod()` — hardcoded opencode CLI commands, volume mounts, env, config paths |
| 8 | `routers/sessions.py:28-36` | Model lists tied to opencode's `provider/model` format |
| 9 | `routers/secrets.py:21` | Secret tab hardcoded as `"opencode"` |

---

## 3. Crush CLI vs OpenCode: Comparison

| Aspect | OpenCode | Crush CLI |
|--------|----------|-----------|
| **Language** | Go | Go |
| **Container Image** | `opencode-golang:latest` (custom-built) | **None published** — must build from binary |
| **Config Format** | `opencode.json` | `crush.json` |
| **Config Path** | `/root/.config/opencode/` | `/root/.config/crush/` |
| **State Path** | `/root/.local/state/opencode/model.json` | `/root/.local/share/crush/crush.json` |
| **Prompt Mode** | `opencode run --model <m> "<prompt>"` | `crush run --model <m> "<prompt>"` |
| **Server Mode** | `opencode serve --hostname 0.0.0.0 --port 4096` | `crush server --host tcp://0.0.0.0:4096` |
| **TUI Mode** | Direct exec (not actually `opencode` in TUI) — `sleep infinity` + kubectl exec | Same pattern: `sleep infinity` + kubectl exec |
| **Continue Flag** | `--continue` | `--continue` / `--session <id>` |
| **Model Format** | `provider/model` or `provider/model@variant` | `provider/model` |
| **Env Vars (Auth)** | `GOOGLE_API_KEY`, `GOOGLE_CLOUD_PROJECT`, `VERTEX_LOCATION`, ADC file | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `VERTEXAI_PROJECT`, `VERTEXAI_LOCATION`, AWS creds, Azure creds |
| **Secret K8s Name** | `opencode-secret` | `crush-secret` (proposed) |
| **Config K8s Name** | `opencode-config` (ConfigMap) | `crush-config` (proposed ConfigMap) |
| **Port (Server Mode)** | 4096 | Configurable, default none — must specify `--host tcp://0.0.0.0:<port>` |
| **Share/History Dir** | `/root/.local/share/opencode` | `/root/.local/share/crush` |
| **Context Files** | n/a | Reads `AGENTS.md`, `CRUSH.md`, `CLAUDE.md` from working dir |
| **MCP Support** | Limited | Full (stdio, http, sse transports) |
| **LSP Support** | No | Yes (auto-discovery + config) |
| **Permission System** | No | Yes (`--yolo` to bypass) |

### Key Differences Affecting Pod Spec

1. **No official Crush container image** — we must create a `Containerfile.crush` that downloads the static binary from GitHub releases
2. **Different config file format and path** — Crush uses `crush.json` at `/root/.config/crush/`
3. **Different server command** — `crush server --host tcp://0.0.0.0:4096` (not `--hostname`)
4. **Different env var names** — Crush supports far more providers natively (Anthropic, OpenAI, Gemini, Bedrock, Azure, etc.)
5. **Model state** — Crush persists model selection in its config service, not a JSON state file
6. **Share directory** — `/root/.local/share/crush` instead of `/root/.local/share/opencode`
7. **`--yolo` flag** — Required for non-interactive prompt mode to skip permission prompts

---

## 4. Proposed Architecture: Agent Tool Abstraction

### 4.1 Design Principle: Strategy Pattern

Introduce an **AgentTool** abstraction that encapsulates everything tool-specific. The core Swarmer codebase becomes tool-agnostic; each agent tool is a pluggable strategy.

```
┌─────────────────────────────────────────────────┐
│                    Swarmer Core                  │
│  (Sessions, Workspaces, K8s, UI, Auth, DB)      │
│                                                  │
│  ┌────────────────────────────────────────────┐  │
│  │          AgentToolRegistry                 │  │
│  │  ┌──────────┐  ┌──────────┐  ┌─────────┐  │  │
│  │  │ OpenCode │  │  Crush   │  │ Future  │  │  │
│  │  │ Strategy │  │ Strategy │  │ Strategy│  │  │
│  │  └──────────┘  └──────────┘  └─────────┘  │  │
│  └────────────────────────────────────────────┘  │
│                                                  │
│  Each strategy provides:                         │
│  • Pod spec builder (commands, volumes, env)     │
│  • Config generator (ConfigMap content)          │
│  • Secret schema (what credentials it needs)     │
│  • Model catalog (available models per creds)    │
│  • Model state writer (exec into running pod)    │
│  • Server port + health check                    │
│  • Container image reference                     │
└─────────────────────────────────────────────────┘
```

### 4.2 Data Model Changes

#### 4.2.1 Session Model — Add `agent_tool` Column

```python
# swarmer/models/session.py

AGENT_TOOLS = ("opencode", "crush")

class Session(Base):
    # ... existing fields ...
    agent_tool: Mapped[str] = mapped_column(
        String(32), nullable=False, default="opencode", server_default="opencode"
    )
```

This column determines which strategy is used for pod spec generation, config generation, and model management for each session.

#### 4.2.2 Rename `OpencodeSecret` → `AgentSecret` (Generalize)

Instead of one model per tool, create a single **generic credential store** that supports all provider types:

```python
# swarmer/models/agent_secret.py  (replaces opencode_secret.py)

class AgentSecret(Base):
    __tablename__ = "agent_secrets"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id"), nullable=False
    )
    # Which provider this credential is for
    provider: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # e.g. "google-vertex", "anthropic", "openai", "gemini", "bedrock"
    
    # All sensitive values encrypted with existing Fernet pattern
    # Key-value pairs serialized as encrypted JSON
    credentials_enc: Mapped[str] = mapped_column(Text, nullable=False, default="")
    
    created_at: Mapped[datetime] = mapped_column(...)
    updated_at: Mapped[datetime] = mapped_column(...)

    __table_args__ = (
        UniqueConstraint("workspace_id", "provider"),
    )
```

This approach:
- Supports arbitrary providers (Anthropic, OpenAI, Google, AWS Bedrock, Azure)
- Each provider's credentials are stored as encrypted JSON blob
- Both OpenCode and Crush can consume the same stored credentials
- New providers don't require schema migrations

#### 4.2.3 Config Table — Add `agent_image_<tool>` to Settings

```python
# swarmer/config.py

class Settings(BaseSettings):
    # ... existing ...
    agent_image_opencode: str = "opencode-golang:latest"
    agent_image_crush: str = "crush:latest"
    default_agent_tool: str = "opencode"
    crush_server_port: int = 4096
```

### 4.3 Agent Tool Strategy Interface

```python
# swarmer/agent_tools/__init__.py

from abc import ABC, abstractmethod

class AgentToolStrategy(ABC):
    """Base class for agent tool integrations."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier, e.g. 'opencode', 'crush'."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name for UI."""

    @abstractmethod
    def get_image(self) -> str:
        """Container image reference for this tool."""

    @abstractmethod
    def get_config_map_name(self) -> str:
        """K8s ConfigMap name for tool config."""

    @abstractmethod
    def build_config(self, secret=None) -> dict[str, str]:
        """Return ConfigMap data dict (filename → content)."""

    @abstractmethod
    def get_secret_name(self) -> str:
        """K8s Secret name for tool credentials."""

    @abstractmethod
    def build_secret_data(self, credentials: dict) -> dict[str, str]:
        """Transform stored credentials → K8s Secret data dict."""

    @abstractmethod
    def build_pod_command(self, session, model: str) -> list[str]:
        """Return the container args for [sh, -c, <command>]."""

    @abstractmethod
    def get_volume_mounts(self) -> list[tuple[str, str, bool]]:
        """Return [(volume_name, mount_path, read_only), ...] for tool-specific mounts."""

    @abstractmethod
    def get_volumes(self, has_adc: bool) -> list:
        """Return K8s volume specs for tool config + credentials."""

    @abstractmethod
    def get_container_name(self) -> str:
        """Main container name in the pod."""

    @abstractmethod
    def get_server_port(self) -> int | None:
        """Port exposed in server mode, or None."""

    @abstractmethod
    def get_share_dir(self) -> str:
        """Path for session history persistence (e.g. /root/.local/share/opencode)."""

    @abstractmethod
    def build_share_setup_cmd(self) -> str:
        """Shell commands to symlink share dir into workspace PVC."""

    @abstractmethod
    def build_model_setup_cmd(self, model: str) -> str:
        """Shell commands to configure the model before the tool starts."""

    @abstractmethod
    def get_model_options(self, credentials: dict) -> list[dict]:
        """Return available model choices based on stored credentials."""

    @abstractmethod
    def exec_model_update(self, pod_name: str, namespace: str, model: str) -> None:
        """Update model selection on a running pod (kubectl exec)."""

    @abstractmethod
    def get_env_vars(self, session, credentials: dict) -> list:
        """Return tool-specific env vars for the main container."""

    @abstractmethod
    def get_env_from(self) -> list:
        """Return envFrom sources (Secret refs)."""
```

### 4.4 OpenCode Strategy Implementation

```python
# swarmer/agent_tools/opencode.py

class OpenCodeStrategy(AgentToolStrategy):
    name = "opencode"
    display_name = "OpenCode"

    def get_image(self) -> str:
        return settings.agent_image_opencode

    def get_config_map_name(self) -> str:
        return "opencode-config"

    def build_config(self, secret=None) -> dict[str, str]:
        # Existing _build_opencode_config() logic
        return {
            "opencode.json": _build_opencode_json(secret),
            "gitconfig": "[safe]\n\tdirectory = *\n",
        }

    def build_pod_command(self, session, model: str) -> list[str]:
        if session.mode == "server":
            return ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"]
        elif session.mode == "tui":
            return ["sleep", "infinity"]
        else:  # prompt
            cmd = ["opencode", "run", "--model", model]
            if session.resume:
                cmd.append("--continue")
            if session.instruction_prompt:
                cmd.append(session.instruction_prompt)
            return cmd

    def get_server_port(self) -> int | None:
        return 4096

    def get_share_dir(self) -> str:
        return "/root/.local/share/opencode"

    # ... etc.
```

### 4.5 Crush Strategy Implementation

```python
# swarmer/agent_tools/crush.py

class CrushStrategy(AgentToolStrategy):
    name = "crush"
    display_name = "Crush"

    def get_image(self) -> str:
        return settings.agent_image_crush

    def get_config_map_name(self) -> str:
        return "crush-config"

    def build_config(self, credentials=None) -> dict[str, str]:
        config = {
            "$schema": "https://charm.land/crush.json",
            "options": {
                "disable_metrics": True,
                "disable_notifications": True,
                "data_directory": ".crush",
            },
        }
        # Configure providers based on available credentials
        if credentials:
            providers = {}
            if credentials.get("anthropic_api_key"):
                pass  # Anthropic is a built-in provider, just needs env var
            if credentials.get("google_api_key"):
                pass  # Gemini is a built-in provider
            if credentials.get("openai_api_key"):
                pass  # OpenAI is a built-in provider
            config["providers"] = providers
        return {
            "crush.json": json.dumps(config, indent=2),
            "gitconfig": "[safe]\n\tdirectory = *\n",
        }

    def build_pod_command(self, session, model: str) -> list[str]:
        if session.mode == "server":
            port = settings.crush_server_port
            return ["crush", "server", "--host", f"tcp://0.0.0.0:{port}"]
        elif session.mode == "tui":
            return ["sleep", "infinity"]
        else:  # prompt
            cmd = ["crush", "run", "--yolo"]  # --yolo required for non-interactive
            if model:
                cmd.extend(["--model", model])
            if session.resume:
                cmd.append("--continue")
            if session.instruction_prompt:
                cmd.append(session.instruction_prompt)
            return cmd

    def get_server_port(self) -> int | None:
        return settings.crush_server_port

    def get_share_dir(self) -> str:
        return "/root/.local/share/crush"

    def build_share_setup_cmd(self) -> str:
        return (
            "mkdir -p /workspace/.crush /root/.local/share && "
            "rm -rf /root/.local/share/crush && "
            "ln -sf /workspace/.crush /root/.local/share/crush && "
        )

    def build_model_setup_cmd(self, model: str) -> str:
        # Crush manages model selection via its config service;
        # write the model into crush.json's models section
        if not model:
            return ""
        config_patch = json.dumps({"models": {"large": model}})
        return (
            "mkdir -p /root/.config/crush && "
            f"printf '%s' {shlex.quote(config_patch)} "
            "> /root/.local/share/crush/crush.json && "
        )

    def get_model_options(self, credentials: dict) -> list[dict]:
        options = []
        # VertexAI is the primary provider for the target audience
        # Available when GCP project + region are configured (with ADC for auth)
        if credentials.get("google_cloud_project") and credentials.get("vertex_location"):
            options.extend([
                {"value": "vertexai/claude-sonnet-4-6", "label": "Claude Sonnet 4.6 (balanced)", "group": "Vertex AI — Claude"},
                {"value": "vertexai/claude-opus-4-6", "label": "Claude Opus 4.6 (most capable)", "group": "Vertex AI — Claude"},
                {"value": "vertexai/claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5 (fast)", "group": "Vertex AI — Claude"},
                {"value": "vertexai/gemini-2.5-pro", "label": "Gemini 2.5 Pro", "group": "Vertex AI — Gemini"},
                {"value": "vertexai/gemini-2.5-flash", "label": "Gemini 2.5 Flash (fast)", "group": "Vertex AI — Gemini"},
            ])
        if credentials.get("anthropic_api_key"):
            options.extend([
                {"value": "anthropic/claude-sonnet-4-6", "label": "Claude Sonnet 4.6", "group": "Anthropic (direct)"},
                {"value": "anthropic/claude-opus-4", "label": "Claude Opus 4", "group": "Anthropic (direct)"},
                {"value": "anthropic/claude-haiku-3.5", "label": "Claude Haiku 3.5 (fast)", "group": "Anthropic (direct)"},
            ])
        if credentials.get("openai_api_key"):
            options.extend([
                {"value": "openai/gpt-4o", "label": "GPT-4o", "group": "OpenAI"},
                {"value": "openai/o3", "label": "o3 (reasoning)", "group": "OpenAI"},
            ])
        if credentials.get("google_api_key") or credentials.get("gemini_api_key"):
            options.extend([
                {"value": "gemini/gemini-2.5-flash", "label": "Gemini 2.5 Flash", "group": "Gemini (AI Studio)"},
                {"value": "gemini/gemini-2.5-pro", "label": "Gemini 2.5 Pro", "group": "Gemini (AI Studio)"},
            ])
        return options

    def get_env_vars(self, session, credentials: dict) -> list:
        # Crush reads standard env vars directly
        # The K8s Secret will contain ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.
        return []  # All injected via envFrom

    def get_env_from(self) -> list:
        from kubernetes import client
        return [
            client.V1EnvFromSource(
                secret_ref=client.V1SecretEnvSource(
                    name="crush-secret", optional=True
                )
            )
        ]

    def exec_model_update(self, pod_name: str, namespace: str, model: str) -> None:
        # Crush doesn't have an equivalent to opencode's model.json state file.
        # For server mode, we'd need to hit the REST API or write config.
        # For TUI mode, the user selects the model interactively.
        pass  # No-op for now; model is set at launch time

    # ... etc.
```

### 4.6 Agent Tool Registry

```python
# swarmer/agent_tools/registry.py

from swarmer.agent_tools.opencode import OpenCodeStrategy
from swarmer.agent_tools.crush import CrushStrategy

_STRATEGIES: dict[str, AgentToolStrategy] = {}


def register(strategy: AgentToolStrategy) -> None:
    _STRATEGIES[strategy.name] = strategy


def get(name: str) -> AgentToolStrategy:
    if name not in _STRATEGIES:
        raise ValueError(f"Unknown agent tool: {name!r}. Available: {list(_STRATEGIES)}")
    return _STRATEGIES[name]


def all_tools() -> list[AgentToolStrategy]:
    return list(_STRATEGIES.values())


# Register built-in strategies
register(OpenCodeStrategy())
register(CrushStrategy())
```

---

## 5. Refactored Module Layout

```
swarmer/
├── agent_tools/                    # NEW — Agent tool abstraction layer
│   ├── __init__.py                 # AgentToolStrategy ABC
│   ├── registry.py                 # Tool registry + registration
│   ├── opencode.py                 # OpenCode strategy
│   └── crush.py                    # Crush strategy
├── models/
│   ├── agent_secret.py             # NEW — Replaces opencode_secret.py
│   ├── opencode_secret.py          # DEPRECATED — kept for migration
│   ├── session.py                  # MODIFIED — add agent_tool column
│   └── ...
├── k8s.py                          # MODIFIED — generalized config/secret helpers
├── k8s_session.py                  # MODIFIED — delegates to strategy.build_pod_*
├── routers/
│   ├── sessions.py                 # MODIFIED — tool selector in create form
│   ├── secrets.py                  # MODIFIED — provider-based credential tabs
│   └── ...
├── templates/
│   ├── secrets/
│   │   ├── tabs.html               # MODIFIED — per-provider credential forms
│   │   ├── provider_form.html      # NEW — generic provider credential form
│   │   └── ...
│   ├── sessions/
│   │   ├── new.html                # MODIFIED — agent tool dropdown
│   │   └── ...
│   └── ...
└── config.py                       # MODIFIED — per-tool image settings
```

---

## 6. Key Refactoring Steps

### Phase A — Abstraction Layer (Non-Breaking)

1. Create `swarmer/agent_tools/` package with `AgentToolStrategy` ABC
2. Implement `OpenCodeStrategy` by extracting existing logic from `k8s.py` and `k8s_session.py`
3. Add `agent_tool` column to `Session` model (default `"opencode"`, migration in `database.py`)
4. Refactor `k8s_session.build_session_pod()` to delegate to the strategy:
   ```python
   def build_session_pod(session, namespace, ...):
       from swarmer.agent_tools.registry import get
       strategy = get(session.agent_tool)
       # Use strategy methods instead of hardcoded opencode logic
   ```
5. Refactor `k8s.apply_opencode_config()` → `k8s.apply_agent_config(namespace, tool_name, secret)`
6. Refactor `k8s.exec_model_json()` → `k8s.exec_model_update(pod_name, namespace, tool_name, model)`
7. Update `config.py` to have `agent_image_opencode` and `agent_image_crush`
8. **Test**: Existing OpenCode sessions must work identically

### Phase B — Crush Integration

1. Create `Containerfile.crush` to build a Crush container image:
   ```dockerfile
   FROM debian:bookworm-slim
   ARG CRUSH_VERSION=latest
   RUN apt-get update && apt-get install -y curl git && \
       curl -fsSL https://charm.sh/install.sh | bash -s -- crush && \
       rm -rf /var/lib/apt/lists/*
   WORKDIR /workspace
   CMD ["sleep", "infinity"]
   ```
2. Implement `CrushStrategy` in `swarmer/agent_tools/crush.py`
3. Add `kind-load-crush` target to Makefile
4. Update session create form with agent tool selector
5. Update secrets page to show provider-appropriate credential forms
6. Add Crush-specific model catalog

### Phase C — Generalized Credential Store

1. Create `AgentSecret` model (workspace_id + provider + encrypted JSON)
2. Write migration to copy existing `OpencodeSecret` data → `AgentSecret` rows
3. Update secrets router to support per-provider CRUD
4. Update `k8s.py` to build K8s Secrets from `AgentSecret` rows
5. Remove deprecated `OpencodeSecret` model

### Phase D — UI Polish

1. Session list: show agent tool badge (OpenCode / Crush)
2. Session detail: tool-specific model selector and options
3. Workspace overview: credential status per provider
4. Session create: tool-aware mode options and defaults

---

## 7. Container Image Strategy

### Crush Container Image

Since Charmbracelet doesn't publish official container images, we need to build our own:

```dockerfile
# Containerfile.crush
FROM debian:bookworm-slim

ARG CRUSH_VERSION=0.1.127
ARG TARGETARCH=amd64

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates curl git openssh-client && \
    curl -fsSL "https://github.com/charmbracelet/crush/releases/download/v${CRUSH_VERSION}/crush_${CRUSH_VERSION}_linux_${TARGETARCH}.tar.gz" \
        | tar -xz -C /usr/local/bin crush && \
    chmod +x /usr/local/bin/crush && \
    rm -rf /var/lib/apt/lists/*

# Crush needs a home directory for config/state
RUN mkdir -p /root/.config/crush /root/.local/share/crush

WORKDIR /workspace
ENV CRUSH_DISABLE_METRICS=1 \
    DO_NOT_TRACK=1

CMD ["sleep", "infinity"]
```

### Makefile Additions

```makefile
CRUSH_IMAGE   ?= crush:latest
CRUSH_VERSION ?= 0.1.127

image-build-crush:  ## Build the Crush agent container image
	$(CONTAINER_CMD) build -f Containerfile.crush \
	  --build-arg CRUSH_VERSION=$(CRUSH_VERSION) \
	  -t $(CRUSH_IMAGE) .

kind-load-crush:  ## Load the Crush agent image into kind
	@echo "Loading $(CRUSH_IMAGE) into kind cluster '$(KIND_CLUSTER)'..."
	@if [ "$(CONTAINER_CMD)" = "podman" ]; then \
	  podman save $(CRUSH_IMAGE) | kind load image-archive /dev/stdin --name $(KIND_CLUSTER); \
	else \
	  kind load docker-image $(CRUSH_IMAGE) --name $(KIND_CLUSTER); \
	fi
```

---

## 8. K8s Resource Naming Convention

To avoid collisions when both tools are used in the same namespace:

| Resource | OpenCode | Crush |
|----------|----------|-------|
| ConfigMap | `opencode-config` | `crush-config` |
| Secret (credentials) | `opencode-secret` | `crush-secret` |
| Container name | `opencode` | `crush` |
| Service port name | `opencode` | `crush` |
| Pod labels | `agent-tool: opencode` | `agent-tool: crush` |

A new label `agent-tool` is added to all session pods so they can be filtered by tool.

---

## 9. Provider Credential Mapping

How stored credentials map to K8s Secret env vars for each tool:

| Provider | Stored Key | OpenCode Env Var | Crush Env Var | Notes |
|----------|-----------|------------------|---------------|-------|
| Google Vertex | `google_cloud_project` | `GOOGLE_CLOUD_PROJECT` | `VERTEXAI_PROJECT` | **Primary provider** — shared by both tools |
| Google Vertex | `vertex_location` | `VERTEX_LOCATION` | `VERTEXAI_LOCATION` | **Primary provider** — shared by both tools |
| Google Vertex | ADC JSON file | `GOOGLE_APPLICATION_CREDENTIALS` (file mount) | `GOOGLE_APPLICATION_CREDENTIALS` (file mount) | Identical mount path; separate K8s Secrets |
| Google AI Studio | `google_api_key` | `GOOGLE_API_KEY` | `GEMINI_API_KEY` | Direct Gemini API (no GCP project needed) |
| Anthropic | `anthropic_api_key` | n/a (not supported) | `ANTHROPIC_API_KEY` | Crush only — direct api.anthropic.com |
| OpenAI | `openai_api_key` | n/a (not supported) | `OPENAI_API_KEY` | Crush only |

> **VertexAI is the most important row.** The target audience primarily uses
> Google Cloud / Vertex AI for Claude access. The same stored GCP Project and
> Region are mapped to different env var names per tool — this is the core
> reason each tool needs its own K8s Secret.
>
> Crush discovers its VertexAI provider automatically when both
> `VERTEXAI_PROJECT` and `VERTEXAI_LOCATION` are set. Auth is handled by
> the Google Cloud Go SDK reading `GOOGLE_APPLICATION_CREDENTIALS` or
> falling back to GKE Workload Identity. No API key is used for VertexAI.
>
> Both tools share the same ADC JSON file (uploaded once in the secrets UI)
> but mount it from their respective K8s Secrets (`opencode-secret` vs
> `crush-secret`).

Each `AgentToolStrategy` implements `build_secret_data()` to map the generic provider credentials to the tool-specific env var names.

---

## 10. Chat Proxy Considerations

### OpenCode Server Mode
- Runs `opencode serve --hostname 0.0.0.0 --port 4096`
- Exposes a web UI that `chat_proxy.py` reverse-proxies
- WebSocket support for real-time communication

### Crush Server Mode
- Runs `crush server --host tcp://0.0.0.0:4096`
- Exposes a **REST API + SSE** (not a web UI)
- Endpoints: `/v1/health`, `/v1/workspaces/{id}/events` (SSE), `/v1/workspaces/{id}/agent` (POST to send prompt)
- No browser-facing UI — Swarmer would need to build its own chat interface that calls the Crush REST API

### Impact on `chat_proxy.py`

The existing proxy assumes a web UI at the target. For Crush, we have two options:

1. **Option A (Recommended)**: Build a thin chat UI in Swarmer templates that calls the Crush REST API directly through the existing proxy. The proxy would forward `/api/crush/...` requests to the Crush server pod.

2. **Option B**: Always use TUI mode for Crush and skip server mode initially. This is simpler but limits functionality.

---

## 11. Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Crush binary size (~50MB) increases pod startup time | Moderate | Pre-pull image; use `IfNotPresent` pull policy |
| No official Crush container image — maintenance burden | Low | Pin to specific release version; automate image builds in CI |
| Crush server mode REST API may change between versions | Moderate | Pin Crush version; integration test the API contract |
| Crush `--yolo` flag in prompt mode skips safety checks | Low (containers are ephemeral) | Document that prompt mode runs without permission gates |
| Database migration from `opencode_secrets` → `agent_secrets` | High if done wrong | Phase the migration; keep both tables during transition; backfill with migration script |
| Session modes (`tui`/`server`/`prompt`) may not map 1:1 | Low | Both tools support all three patterns; strategy handles differences |

---

## 12. Future Extensibility

This architecture easily supports additional tools:

1. **Aider** — another popular CLI coding assistant
2. **Claude Code** — Anthropic's official CLI agent
3. **Codex CLI** — OpenAI's CLI agent
4. **Custom/internal tools** — any containerized CLI that follows the pattern

To add a new tool:
1. Create `swarmer/agent_tools/<tool>.py` implementing `AgentToolStrategy`
2. Call `registry.register()` in `__init__.py`
3. Build and publish a container image
4. Add `agent_image_<tool>` to `config.py`
5. Add Makefile targets for image build/load

No database migrations, no router changes, no template changes needed for the core flow.

---

## 13. Implementation Priority

| Priority | Task | Effort |
|----------|------|--------|
| P0 | Create `agent_tools/` abstraction + OpenCode strategy (extract existing code) | 2-3 days |
| P0 | Add `agent_tool` column to Session model + migration | 0.5 day |
| P1 | Build Crush container image (`Containerfile.crush`) | 0.5 day |
| P1 | Implement Crush strategy | 1-2 days |
| P1 | Update session create/edit forms with tool selector | 1 day |
| P2 | Generalize credential store (`AgentSecret` model) | 2 days |
| P2 | Provider-aware secrets UI | 1-2 days |
| P3 | Crush server mode chat proxy integration | 2-3 days |
| P3 | UI polish (tool badges, tool-specific options) | 1 day |

**Total estimated effort: 11-15 days**

---

## 14. Summary

The core insight is that **OpenCode and Crush follow the same fundamental pattern**: a containerized Go binary that can run in prompt, server, or TUI mode, consuming API keys from environment variables and configuration from a JSON file. The differences are in the specifics (command flags, config paths, env var names, server API shape).

By introducing the `AgentToolStrategy` abstraction:
- All tool-specific knowledge is encapsulated in strategy classes
- The rest of Swarmer (sessions, workspaces, K8s lifecycle, UI) becomes tool-agnostic
- Adding new tools is a ~1 day exercise of implementing the strategy interface
- Existing OpenCode functionality is preserved with zero behavior change
