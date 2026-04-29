# Execution Plan — Multi-Agent-Tool Support

**Date:** 2026-04-15
**Depends on:** `2026-04-15-08-multi-agent-tool-architecture.md`
**Status:** Ready for implementation
**PR:** [#4 feat: add Crush CLI agent tool support](https://github.com/stolostron/agent-swarm/pull/4)

---

## Overview

Five sequential phases, each producing a working system at its boundary:

| Phase | Goal | Test gate |
|-------|------|-----------|
| **1** | Create `AgentToolStrategy` ABC + `OpenCodeStrategy` (extract, don't change behavior) | Existing OpenCode sessions launch identically |
| **2** | Wire `agent_tool` column into Session model, routers, and templates | UI shows "Agent Tool" selector; OpenCode is the only choice and still works |
| **3** | Generalize secrets to support Crush providers (Anthropic, OpenAI) while preserving VertexAI as a first-class shared provider | New credential forms render; OpenCode and VertexAI secrets still sync to K8s |
| **4** | Build Crush container image + implement `CrushStrategy` + wire into UI | Crush sessions launch, run prompts, connect via TUI |
| **5** | End-to-end validation of Crush in all three modes | Crush prompt/server/TUI all produce expected behavior |

Each phase lists **every file to create or modify**, the **exact changes** within each file, and a **verification step**.

---

## Phase 1 — AgentToolStrategy ABC + OpenCode Extraction

**Goal:** Create the abstraction layer and move all OpenCode-specific logic into an `OpenCodeStrategy` class. Zero behavior change.

### Step 1.1 — Create `swarmer/agent_tools/__init__.py`

Create the abstract base class.

```python
"""
Agent tool abstraction layer.

Each supported CLI tool (opencode, crush, …) implements AgentToolStrategy.
The rest of Swarmer is tool-agnostic and delegates tool-specific behavior
to the strategy obtained from the registry.
"""
from abc import ABC, abstractmethod


class AgentToolStrategy(ABC):
    """Interface that every agent tool must implement."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier stored in Session.agent_tool, e.g. 'opencode'."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable label shown in the UI."""

    @abstractmethod
    def get_image(self) -> str:
        """Container image reference for session pods."""

    @abstractmethod
    def get_config_map_name(self) -> str:
        """Name of the K8s ConfigMap that holds tool configuration."""

    @abstractmethod
    def build_config_data(self, secret=None) -> dict[str, str]:
        """Return {filename: content} for the tool's ConfigMap."""

    @abstractmethod
    def get_config_mount_path(self) -> str:
        """Container path where the ConfigMap is mounted."""

    @abstractmethod
    def get_secret_name(self) -> str:
        """Name of the K8s Secret that holds provider credentials."""

    @abstractmethod
    def get_container_name(self) -> str:
        """Name of the main container inside the pod."""

    @abstractmethod
    def get_server_port(self) -> int | None:
        """Port exposed in server mode, or None if server mode is not supported."""

    @abstractmethod
    def get_share_dir(self) -> str:
        """Host path for session history, e.g. '/root/.local/share/opencode'."""

    @abstractmethod
    def build_share_setup_cmd(self) -> str:
        """Shell fragment that symlinks the share dir into /workspace."""

    @abstractmethod
    def build_model_setup_cmd(self, model: str) -> str:
        """Shell fragment that configures the active model before the tool starts."""

    @abstractmethod
    def build_main_cmd(self, session, model: str) -> str:
        """Shell command string for the main container (after share+model setup)."""

    @abstractmethod
    def get_server_mode_ports(self) -> list:
        """Return list of client.V1ContainerPort for server mode, or []."""

    @abstractmethod
    def get_model_options(self, secret=None) -> list[dict]:
        """Return [{"value": ..., "label": ..., "group": ...}, ...]."""

    @abstractmethod
    def get_default_model(self, has_adc: bool, has_gemini: bool) -> str:
        """Return the default model ID given available credentials."""

    @abstractmethod
    def exec_model_update(self, pod_name: str, namespace: str, model: str) -> None:
        """Update model selection on a running pod (kubectl exec). No-op if unsupported."""

    @abstractmethod
    def get_env_from_sources(self) -> list:
        """Return list of client.V1EnvFromSource for the main container."""

    @abstractmethod
    def get_extra_env(self, has_adc: bool) -> list:
        """Return list of client.V1EnvVar specific to this tool (e.g. GOOGLE_APPLICATION_CREDENTIALS)."""

    @abstractmethod
    def get_extra_volumes(self, has_adc: bool) -> list:
        """Return list of client.V1Volume for tool-specific mounts (ADC file, etc.)."""

    @abstractmethod
    def get_extra_volume_mounts(self, has_adc: bool) -> list:
        """Return list of client.V1VolumeMount for tool-specific mounts."""

    @abstractmethod
    def build_k8s_secret_data(self, secret) -> dict[str, str]:
        """Transform an OpencodeSecret (or future AgentSecret) into K8s Secret .data dict."""
```

### Step 1.2 — Create `swarmer/agent_tools/opencode.py`

Extract all OpenCode-specific logic from `k8s.py` and `k8s_session.py` into this strategy. Every method body comes from existing, tested code — it is being _moved_, not rewritten.

Source mapping:

| Method | Extracted from |
|--------|----------------|
| `get_image()` | `config.py:12` — `settings.agent_image` |
| `build_config_data()` | `k8s.py:76-104` — `_build_opencode_config()` |
| `get_config_mount_path()` | `k8s_session.py:148-149` — hardcoded `/root/.config/opencode` |
| `build_share_setup_cmd()` | `k8s_session.py:270-274` — `share_setup` string |
| `build_model_setup_cmd()` | `k8s_session.py:253-266` — `model_setup` string |
| `build_main_cmd()` | `k8s_session.py:278-293` — the `if session.mode == ...` block |
| `get_model_options()` | `routers/sessions.py:28-52` — `_GEMINI_MODELS`, `_CLAUDE_MODELS`, `_get_model_options()` |
| `get_default_model()` | `k8s_session.py:241-248` — model resolution logic |
| `exec_model_update()` | `k8s.py:337-364` — `exec_model_json()` |
| `get_env_from_sources()` | `k8s_session.py:298-304` — `env_from` list |
| `get_extra_env()` | `k8s_session.py:96-99` — `GOOGLE_APPLICATION_CREDENTIALS` env var |
| `get_extra_volumes()` | `k8s_session.py:153-174` — `gcloud-creds` volume |
| `get_extra_volume_mounts()` | `k8s_session.py:168-174` — `/app/gcloud` mount |
| `build_k8s_secret_data()` | `k8s.py:167-178` — `apply_opencode_secret()` body |

The strategy class must import `from swarmer.config import settings` to read `settings.agent_image` (soon to be `settings.agent_image_opencode`).

### Step 1.3 — Create `swarmer/agent_tools/registry.py`

```python
from swarmer.agent_tools import AgentToolStrategy

_REGISTRY: dict[str, AgentToolStrategy] = {}


def register(strategy: AgentToolStrategy) -> None:
    _REGISTRY[strategy.name] = strategy


def get(name: str) -> AgentToolStrategy:
    if name not in _REGISTRY:
        raise ValueError(
            f"Unknown agent tool: {name!r}. Available: {list(_REGISTRY)}"
        )
    return _REGISTRY[name]


def all_tools() -> list[AgentToolStrategy]:
    return list(_REGISTRY.values())


def _init() -> None:
    from swarmer.agent_tools.opencode import OpenCodeStrategy
    register(OpenCodeStrategy())


_init()
```

### Step 1.4 — Refactor `swarmer/k8s_session.py:build_session_pod()`

Replace the hardcoded OpenCode logic with strategy delegation. The function signature stays the same to avoid breaking callers.

**Changes inside `build_session_pod()`:**

1. Add `agent_tool: str = "opencode"` parameter (with backward-compatible default).
2. At the top of the function body: `from swarmer.agent_tools.registry import get as get_tool; tool = get_tool(agent_tool)`.
3. Replace hardcoded config volume with `tool.get_config_map_name()` and `tool.get_config_mount_path()`.
4. Replace `env_from` block with `tool.get_env_from_sources()`.
5. Replace `share_setup` string with `tool.build_share_setup_cmd()`.
6. Replace model resolution block with `tool.get_default_model(has_adc, has_gemini)`.
7. Replace `model_setup` string with `tool.build_model_setup_cmd(model)`.
8. Replace `main_cmd` block with `tool.build_main_cmd(session, model)`.
9. Replace `ports` assignment with `tool.get_server_mode_ports()` (when `session.mode == "server"`).
10. Replace `gcloud-creds` volume/mount with `tool.get_extra_volumes(has_adc)` and `tool.get_extra_volume_mounts(has_adc)`.
11. Replace `GOOGLE_APPLICATION_CREDENTIALS` env with `tool.get_extra_env(has_adc)`.
12. Replace container `name="opencode"` with `tool.get_container_name()`.
13. Replace `image=image` with `image=tool.get_image()` (drop the `image` parameter entirely, or keep for override).

### Step 1.5 — Refactor `swarmer/k8s.py`

1. **Rename `apply_opencode_config()` → `apply_agent_config()`**: Take `agent_tool: str` parameter, call `get_tool(agent_tool).build_config_data(secret)` and `get_tool(agent_tool).get_config_map_name()`.
2. **Rename `exec_model_json()` → `exec_model_update()`**: Delegate to `get_tool(agent_tool).exec_model_update()`.
3. **Rename `apply_opencode_secret()` → `apply_agent_secret()`**: Delegate to `get_tool(agent_tool).build_k8s_secret_data()`.
4. Keep the old function names as thin wrappers that call the new functions with `agent_tool="opencode"` to avoid breaking the rest of the codebase during this phase. Remove the wrappers in Phase 2.

### Step 1.6 — Update callers

Update every call site to use the new names (or verify the wrappers work):

| File | Line(s) | Change |
|------|---------|--------|
| `routers/sessions.py:376` | `k8s_sess.build_session_pod(...)` | Add `agent_tool=session.agent_tool` (once column exists in Phase 2; for now leave default) |
| `routers/sessions.py:649` | `k8s.exec_model_json(...)` | Will become `k8s.exec_model_update(...)` |
| `routers/secrets.py:143-144` | `k8s.apply_opencode_secret(...)` + `k8s.apply_opencode_config(...)` | Will become `k8s.apply_agent_secret(...)` + `k8s.apply_agent_config(...)` |
| `routers/workspaces.py` (if any) | `k8s.apply_opencode_config(...)` on workspace creation | Same |

### Step 1.7 — Verification

```sh
make lint                    # No ruff errors
make dev                     # Start the dashboard
# Create a workspace → create a session → launch → verify pod runs opencode
# Model selector shows same options as before
# Server mode + TUI mode still work
```

**Gate:** All existing functionality is identical. No UI changes visible.

---

## Phase 2 — `agent_tool` Column + UI Selector

**Goal:** Sessions carry an `agent_tool` field. The UI exposes it as a dropdown. Only "OpenCode" appears for now.

### Step 2.1 — Add column to Session model

**File:** `swarmer/models/session.py`

```python
AGENT_TOOLS = ("opencode", "crush")

class Session(Base):
    # After the existing `privileged` column:
    agent_tool: Mapped[str] = mapped_column(
        String(32), nullable=False, default="opencode", server_default="opencode"
    )
```

### Step 2.2 — Add migration

**File:** `swarmer/database.py` — append to `migrations` list:

```python
"ALTER TABLE sessions ADD COLUMN agent_tool VARCHAR(32) NOT NULL DEFAULT 'opencode'",
```

### Step 2.3 — Update `swarmer/config.py`

Replace the single `agent_image` with per-tool settings:

```python
class Settings(BaseSettings):
    # ... existing fields ...
    # Replace:  agent_image: str = "opencode-golang:latest"
    agent_image_opencode: str = "opencode-golang:latest"
    agent_image_crush: str = "crush:latest"
    default_agent_tool: str = "opencode"
    crush_server_port: int = 4096
    # Keep agent_image_pull_secret as-is (shared across tools)
```

### Step 2.4 — Update `.env.example`

```
# Images used when launching session pods (one per agent tool)
AGENT_IMAGE_OPENCODE=opencode-golang:latest
AGENT_IMAGE_CRUSH=crush:latest
DEFAULT_AGENT_TOOL=opencode
```

### Step 2.5 — Update `swarmer/routers/sessions.py`

**session_new (GET):** Pass `agent_tools` list to template context:

```python
from swarmer.agent_tools.registry import all_tools
# In session_new():
    return templates.TemplateResponse(
        "sessions/new.html",
        {
            ...,
            "agent_tools": all_tools(),
            "default_agent_tool": settings.default_agent_tool,
        },
    )
```

**session_create (POST):** Accept `agent_tool` form field:

```python
async def session_create(
    ...,
    agent_tool: str = Form("opencode"),
    ...
):
    from swarmer.agent_tools.registry import get as get_tool
    try:
        get_tool(agent_tool)
    except ValueError:
        agent_tool = "opencode"

    session = Session(
        ...,
        agent_tool=agent_tool,
    )
```

**session_edit (POST):** Accept and save `agent_tool`:

```python
async def session_edit(
    ...,
    agent_tool: str = Form("opencode"),
    ...
):
    from swarmer.agent_tools.registry import get as get_tool
    try:
        get_tool(agent_tool)
        session.agent_tool = agent_tool
    except ValueError:
        pass
```

**session_launch (POST):** Pass `agent_tool` to pod builder:

```python
pod_spec = k8s_sess.build_session_pod(
    ...,
    agent_tool=session.agent_tool,
)
```

And use the strategy for the service port:

```python
tool = get_tool(session.agent_tool)
if session.mode == "server":
    k8s_sess.create_session_service(session.id, ws.namespace, port=tool.get_server_port())
```

**_get_model_options():** Delegate to strategy:

```python
async def _get_model_options(ws_id: int, db: AsyncSession, agent_tool: str = "opencode") -> list[dict]:
    from swarmer.agent_tools.registry import get as get_tool
    tool = get_tool(agent_tool)
    result = await db.execute(
        select(OpencodeSecret).where(OpencodeSecret.workspace_id == ws_id)
    )
    oc = result.scalar_one_or_none()
    return tool.get_model_options(oc)
```

**session_set_model (POST):** Use strategy for exec:

```python
tool = get_tool(session.agent_tool)
tool.exec_model_update(session.pod_name, ws.namespace, session.model)
```

**session_detail (GET) and session_status (GET):** Pass `agent_tool` label to template:

```python
from swarmer.agent_tools.registry import get as get_tool
tool = get_tool(session.agent_tool)
# Add to context:
"agent_tool_label": tool.display_name,
```

### Step 2.6 — Update `swarmer/k8s_session.py:create_session_service()`

Add a `port` parameter (default `4096`) so it can be driven by the strategy:

```python
def create_session_service(session_id: int, namespace: str, port: int = 4096) -> str:
    # ... existing code, but use `port` variable instead of hardcoded 4096
```

### Step 2.7 — Update `swarmer/routers/secrets.py`

The `apply_opencode_config` call on save needs the agent_tool:

```python
k8s.apply_agent_config(ws.namespace, secret=secret, agent_tool="opencode")
```

For now, hardcode `"opencode"` — in Phase 3, the secrets page will become provider-aware.

### Step 2.8 — Update `chat_proxy.py`

The hardcoded port `4096` in `_acquire_portforward()` must come from the session's strategy:

```python
async def _acquire_portforward(session_id: int, pod_name: str, namespace: str, port: int = 4096) -> int:
    # Change f"{local_port}:4096" → f"{local_port}:{port}"
```

Callers pass `port=tool.get_server_port()`.

### Step 2.9 — Template changes

**`sessions/new.html`** — Add agent tool selector before Mode:

```html
<!-- Agent Tool -->
<div class="mb-3">
  <label class="form-label fw-semibold">Agent Tool</label>
  <select class="form-select" name="agent_tool" id="agent-tool">
    {% for tool in agent_tools %}
    <option value="{{ tool.name }}"
            {% if tool.name == default_agent_tool %}selected{% endif %}>
      {{ tool.display_name }}
    </option>
    {% endfor %}
  </select>
</div>
```

Update the mode `<option>` text to remove "opencode" references:

```html
<option value="prompt">Prompt — run a one-shot prompt, then exit</option>
<option value="server">Server — persistent API access</option>
<option value="tui">TUI — interactive terminal in the browser</option>
```

Update the instruction prompt help text:

```html
<div class="form-text">
  The prompt passed to the agent in prompt mode.
</div>
```

Update the resume checkbox label:

```html
<label class="form-check-label" for="resume">
  Resume last session on re-launch
</label>
<div class="form-text">Passes <code>--continue</code> to the agent.</div>
```

**`sessions/list.html`** — Add "Tool" column:

```html
<th>Tool</th>
<!-- In the row: -->
<td><span class="badge bg-dark">{{ s.agent_tool | capitalize }}</span></td>
```

Update the empty-state text to remove "opencode" reference:

```html
<p class="text-muted">
  Sessions represent a PVC-backed workspace where git repos are cloned
  and the agent tool runs.
</p>
```

**`sessions/detail.html`** — Add agent tool display row in the Configuration card:

```html
<div class="row align-items-center mb-3">
  <label class="col-5 text-muted small col-form-label-sm">Agent Tool</label>
  <div class="col-7">
    <select name="agent_tool" class="form-select form-select-sm"
            {% if session.is_active %}disabled title="Stop session to change tool"{% endif %}>
      {% for tool in agent_tools %}
      <option value="{{ tool.name }}"
              {{ 'selected' if session.agent_tool == tool.name }}>
        {{ tool.display_name }}
      </option>
      {% endfor %}
    </select>
  </div>
</div>
```

Update the instruction prompt placeholder to remove "opencode":

```
placeholder="Describe the task for the agent to run…"
```

**`sessions/_status_badge.html`** — No changes needed.

**`workspaces/detail.html`** — Add "Tool" column to the sessions table:

```html
<th>Tool</th>
<!-- In the row: -->
<td><span class="badge bg-dark">{{ s.agent_tool | capitalize }}</span></td>
```

### Step 2.10 — Remove the backward-compatibility wrappers from Step 1.5

Now that all callers use the new names, delete:
- `apply_opencode_config()` wrapper in `k8s.py` (keep only `apply_agent_config()`)
- `exec_model_json()` wrapper (keep only `exec_model_update()`)
- `apply_opencode_secret()` wrapper (keep only `apply_agent_secret()`)

### Step 2.11 — Pass `agent_tools` context everywhere needed

The `session_detail` handler must also pass `agent_tools` to the template so the detail-page `<select>` can render. Same for `session_edit`.

### Step 2.12 — Verification

```sh
make lint
make dev
# Verify:
# 1. Session create form shows "Agent Tool" dropdown with "OpenCode" selected
# 2. Session list shows "Tool" column
# 3. Session detail shows agent tool in configuration card
# 4. Creating + launching a session still works (same OpenCode behavior)
# 5. Edit → change tool (only OpenCode available) → save → verify
# 6. Workspace detail shows tool column
```

**Gate:** UI shows agent tool selector. Only OpenCode available. All existing flows work.

---

## Phase 3 — Generalize Secrets for Multi-Provider Support

**Goal:** The secrets page supports Crush-relevant providers (Anthropic, OpenAI) alongside the existing Google/Vertex AI credentials. VertexAI is the primary provider for the target audience and must remain first-class — its existing GCP Project, Region, and ADC fields are shared by both OpenCode and Crush. The `OpencodeSecret` model is kept as-is but the UI and K8s sync become provider-aware.

> **Design note — VertexAI is shared infrastructure, not tool-specific.**
> Both OpenCode and Crush consume VertexAI credentials, but with different env var names:
> - OpenCode: `GOOGLE_CLOUD_PROJECT`, `VERTEX_LOCATION`, `GOOGLE_APPLICATION_CREDENTIALS`
> - Crush: `VERTEXAI_PROJECT`, `VERTEXAI_LOCATION`, `GOOGLE_APPLICATION_CREDENTIALS`
>
> The `build_k8s_secret_data()` method in each strategy maps the same stored DB fields to the correct env var names for its K8s Secret. ADC (Application Default Credentials) JSON is mounted as a file volume and `GOOGLE_APPLICATION_CREDENTIALS` is identical for both tools.

### Step 3.1 — Add new encrypted fields to `OpencodeSecret`

Rather than creating a new `AgentSecret` model (which requires a full data migration), extend the existing model with new optional fields. This is the lowest-risk approach.

**File:** `swarmer/models/opencode_secret.py`

Rename the class and table is too risky for existing databases. Instead, add columns:

```python
# New encrypted fields for additional providers
anthropic_api_key_enc: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
openai_api_key_enc: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

# Transparent accessors (same pattern as google_api_key)
@property
def anthropic_api_key(self) -> str: ...
@anthropic_api_key.setter
def anthropic_api_key(self, value: str) -> None: ...

@property
def openai_api_key(self) -> str: ...
@openai_api_key.setter
def openai_api_key(self, value: str) -> None: ...

@property
def masked_anthropic_key(self) -> str: ...

@property
def masked_openai_key(self) -> str: ...

# Convenience helpers for provider detection across tools
@property
def has_vertex(self) -> bool:
    """True when both GCP project and region are set (with or without ADC)."""
    return bool(self.google_cloud_project and self.vertex_location)

@property
def has_anthropic(self) -> bool:
    return bool(self.anthropic_api_key_enc)

@property
def has_openai(self) -> bool:
    return bool(self.openai_api_key_enc)
```

The existing `has_adc` property is preserved. `has_vertex` is a new convenience that checks whether the GCP project + region pair is configured (required for both OpenCode and Crush VertexAI support). `has_adc` remains separate because ADC is optional — VertexAI can also auth via workload identity in GKE.

### Step 3.2 — Add migrations

**File:** `swarmer/database.py` — append:

```python
"ALTER TABLE opencode_secrets ADD COLUMN anthropic_api_key_enc TEXT NOT NULL DEFAULT ''",
"ALTER TABLE opencode_secrets ADD COLUMN openai_api_key_enc TEXT NOT NULL DEFAULT ''",
```

### Step 3.3 — Update `swarmer/routers/secrets.py`

Add the new fields to `opencode_secret_save()`:

```python
async def opencode_secret_save(
    ...,
    anthropic_api_key: str = Form(""),
    openai_api_key: str = Form(""),
    ...
):
    ...
    if anthropic_api_key.strip():
        secret.anthropic_api_key = anthropic_api_key.strip()
    if openai_api_key.strip():
        secret.openai_api_key = openai_api_key.strip()
```

Rename the tab label from "OpenCode" to "API Credentials":

```python
_VALID_TABS = ("credentials", "pats", "pull-secret")
```

Update the redirect in `opencode_secret_save` to `?tab=credentials`.

### Step 3.4 — Update `swarmer/templates/secrets/tabs.html`

Rename the "OpenCode" tab to "API Credentials". Reorganize the card layout into
three rows to give VertexAI the prominence it deserves as the primary provider
for the target audience:

```html
<li class="nav-item">
  <a class="nav-link {% if tab == 'credentials' %}active{% endif %}"
     href="?tab=credentials">API Credentials</a>
</li>
```

Update the description text to explain provider coverage:

```html
<p class="text-muted mb-4">
  API credentials for AI providers used by OpenCode and Crush.
  Stored encrypted; synced to tool-specific K8s secrets in namespace
  <code>{{ ws.namespace }}</code>.
  <strong>Google Cloud / Vertex AI</strong> credentials are shared across both tools
  and enable Claude (via Vertex) and Gemini models.
</p>
```

**Row 1 — Google Cloud / Vertex AI (shared, primary):**

Keep the existing two-column card layout (GCP Project + Region on the left,
ADC upload on the right) **unchanged**. This is already the most important
credential set. Update the card header from "Claude (via Vertex AI)" to
"Google Cloud / Vertex AI" and fold the GCP project, region, and ADC
fields into a single card. Add a form-text note:

```html
<div class="form-text text-muted">
  <strong>Used by both OpenCode and Crush.</strong>
  Enables Claude (Anthropic on Vertex) and Gemini models.
  For Crush, these map to <code>VERTEXAI_PROJECT</code> and
  <code>VERTEXAI_LOCATION</code>.
  For OpenCode, they map to <code>GOOGLE_CLOUD_PROJECT</code> and
  <code>VERTEX_LOCATION</code>.
</div>
```

**Row 2 — Google AI Studio (Gemini direct):**

Keep existing Gemini API key card **unchanged**.

**Row 3 — Additional providers (new):**

Add Anthropic and OpenAI cards after the Gemini card:

```html
<div class="col-md-6">
  <div class="card h-100">
    <div class="card-header fw-semibold">Anthropic (direct API)</div>
    <div class="card-body">
      <div class="mb-3">
        <label class="form-label">API Key</label>
        <input type="password" class="form-control" name="anthropic_api_key"
               placeholder="{{ secret.masked_anthropic_key if (secret and secret.anthropic_api_key_enc) else 'Enter API key' }}"
               autocomplete="off">
        {% if secret and secret.anthropic_api_key_enc %}
        <div class="form-text text-muted">
          Current: <code>{{ secret.masked_anthropic_key }}</code> — leave blank to keep.
        </div>
        {% endif %}
        <div class="form-text text-muted">
          Direct Anthropic API key (api.anthropic.com). Only needed if you don't
          use Vertex AI for Claude access. <strong>Crush only</strong> — OpenCode
          does not support the direct Anthropic API.
        </div>
      </div>
    </div>
  </div>
</div>

<div class="col-md-6">
  <div class="card h-100">
    <div class="card-header fw-semibold">OpenAI</div>
    <div class="card-body">
      <div class="mb-3">
        <label class="form-label">API Key</label>
        <input type="password" class="form-control" name="openai_api_key"
               placeholder="{{ secret.masked_openai_key if (secret and secret.openai_api_key_enc) else 'Enter API key' }}"
               autocomplete="off">
        {% if secret and secret.openai_api_key_enc %}
        <div class="form-text text-muted">
          Current: <code>{{ secret.masked_openai_key }}</code> — leave blank to keep.
        </div>
        {% endif %}
        <div class="form-text text-muted">
          <strong>Crush only</strong> — OpenCode does not support OpenAI models.
        </div>
      </div>
    </div>
  </div>
</div>
```

**Provider coverage summary (display in a small table below the form):**

```html
<div class="mt-4">
  <details class="text-muted small">
    <summary>Provider compatibility</summary>
    <table class="table table-sm table-bordered mt-2" style="max-width:500px">
      <thead><tr><th>Provider</th><th>OpenCode</th><th>Crush</th></tr></thead>
      <tbody>
        <tr><td>Google Cloud / Vertex AI</td><td>✓</td><td>✓</td></tr>
        <tr><td>Gemini (AI Studio)</td><td>✓</td><td>✓</td></tr>
        <tr><td>Anthropic (direct)</td><td>—</td><td>✓</td></tr>
        <tr><td>OpenAI</td><td>—</td><td>✓</td></tr>
      </tbody>
    </table>
  </details>
</div>
```

### Step 3.5 — Update K8s secret sync

**File:** `swarmer/k8s.py`

The `apply_agent_secret()` function (formerly `apply_opencode_secret()`) must also sync Crush-specific credentials to a separate `crush-secret`:

```python
def apply_agent_secret(namespace: str, secret, agent_tool: str = "opencode") -> None:
    from swarmer.agent_tools.registry import get as get_tool
    tool = get_tool(agent_tool)
    data = tool.build_k8s_secret_data(secret)
    _apply_secret(namespace, tool.get_secret_name(), data)
```

Call this for **each registered tool** when saving credentials, so both `opencode-secret` and `crush-secret` are kept in sync:

```python
def sync_all_agent_secrets(namespace: str, secret) -> None:
    from swarmer.agent_tools.registry import all_tools
    for tool in all_tools():
        data = tool.build_k8s_secret_data(secret)
        if data:  # Only create the secret if there are credentials to store
            _apply_secret(namespace, tool.get_secret_name(), data)
```

### Step 3.6 — Implement `build_k8s_secret_data()` in OpenCodeStrategy

This already exists from Phase 1. Verify it maps:
- `GOOGLE_CLOUD_PROJECT`, `VERTEX_LOCATION`, `GOOGLE_API_KEY`, `application_default_credentials.json`

### Step 3.7 — Update `secrets.py` router save handler

Replace the direct `k8s.apply_opencode_secret()` call with `k8s.sync_all_agent_secrets()`.

### Step 3.8 — Verification

```sh
make lint
make dev
# Verify:
# 1. Secrets page shows "API Credentials" tab (not "OpenCode")
# 2. Anthropic and OpenAI key fields appear in new row below Vertex/Gemini
# 3. Saving an Anthropic key encrypts and persists it
# 4. Existing Google Cloud / Vertex AI credentials still work
# 5. Vertex AI card header and help text explain shared usage across tools
# 6. Provider compatibility table renders in collapsible details
# 7. K8s secrets are created/updated in the namespace:
#    - opencode-secret contains GOOGLE_CLOUD_PROJECT, VERTEX_LOCATION, GOOGLE_API_KEY
#    - crush-secret contains VERTEXAI_PROJECT, VERTEXAI_LOCATION, GEMINI_API_KEY
#    - Both contain application_default_credentials.json if ADC uploaded
#    - crush-secret also contains ANTHROPIC_API_KEY, OPENAI_API_KEY if set
# 8. OpenCode sessions still launch correctly with existing Vertex credentials
```

**Gate:** Multi-provider credential storage works. VertexAI remains first-class. No functional regression.

---

## Phase 4 — Crush Container Image + CrushStrategy + UI Integration

**Goal:** Crush sessions can be created and launched.

### Step 4.1 — Create `Containerfile.crush`

**File:** `agent-swarm/Containerfile.crush`

```dockerfile
FROM debian:bookworm-slim

ARG CRUSH_VERSION=0.1.127
ARG TARGETARCH=amd64

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates curl git openssh-client && \
    curl -fsSL \
        "https://github.com/charmbracelet/crush/releases/download/v${CRUSH_VERSION}/crush_${CRUSH_VERSION}_linux_${TARGETARCH}.tar.gz" \
        | tar -xz -C /usr/local/bin crush && \
    chmod +x /usr/local/bin crush && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p /root/.config/crush /root/.local/share/crush

WORKDIR /workspace
ENV CRUSH_DISABLE_METRICS=1 \
    DO_NOT_TRACK=1

CMD ["sleep", "infinity"]
```

### Step 4.2 — Add Makefile targets

**File:** `agent-swarm/Makefile`

Add after the existing `kind-load-opencode` target:

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
	@echo "✓ Crush image loaded."
```

Update `.PHONY` to include `image-build-crush kind-load-crush`.

### Step 4.3 — Create `swarmer/agent_tools/crush.py`

Implement `CrushStrategy`. Key differences from OpenCode:

```python
import json
import shlex

from swarmer.agent_tools import AgentToolStrategy
from swarmer.config import settings


class CrushStrategy(AgentToolStrategy):
    @property
    def name(self) -> str:
        return "crush"

    @property
    def display_name(self) -> str:
        return "Crush"

    def get_image(self) -> str:
        return settings.agent_image_crush

    def get_config_map_name(self) -> str:
        return "crush-config"

    def build_config_data(self, secret=None) -> dict[str, str]:
        config = {
            "$schema": "https://charm.land/crush.json",
            "options": {
                "disable_metrics": True,
                "disable_notifications": True,
                "data_directory": ".crush",
            },
        }
        return {
            "crush.json": json.dumps(config, indent=2),
            "gitconfig": "[safe]\n\tdirectory = *\n",
        }

    def get_config_mount_path(self) -> str:
        return "/root/.config/crush"

    def get_secret_name(self) -> str:
        return "crush-secret"

    def get_container_name(self) -> str:
        return "crush"

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
        # Crush reads model from config; set it in the data dir config
        if not model:
            return ""
        config_data = json.dumps({"models": {"large": model}})
        return (
            "mkdir -p /root/.local/share/crush && "
            f"printf '%s' {shlex.quote(config_data)} "
            "> /root/.local/share/crush/crush.json && "
        )

    def build_main_cmd(self, session, model: str) -> str:
        if session.mode == "server":
            port = settings.crush_server_port
            return f"crush server --host tcp://0.0.0.0:{port}"
        elif session.mode == "tui":
            return "sleep infinity"
        else:  # prompt
            cmd_parts = ["crush", "run", "--yolo"]
            if model:
                cmd_parts.extend(["--model", model])
            if session.resume:
                cmd_parts.append("--continue")
            if session.instruction_prompt:
                cmd_parts.append(session.instruction_prompt)
            return " ".join(shlex.quote(p) for p in cmd_parts)

    def get_server_mode_ports(self) -> list:
        from kubernetes import client
        port = settings.crush_server_port
        return [client.V1ContainerPort(container_port=port, name="crush")]

    def get_model_options(self, secret=None) -> list[dict]:
        options = []
        # VertexAI models (primary provider for target audience)
        # Crush uses provider ID "vertexai" — model IDs are bare names
        # (the provider prefix is added by the UI as "vertexai/<model>")
        if secret and secret.has_adc:
            options.extend([
                {"value": "vertexai/claude-sonnet-4-6", "label": "Claude Sonnet 4.6 (balanced)", "group": "Vertex AI — Claude"},
                {"value": "vertexai/claude-opus-4-6", "label": "Claude Opus 4.6 (most capable)", "group": "Vertex AI — Claude"},
                {"value": "vertexai/claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5 (fast)", "group": "Vertex AI — Claude"},
                {"value": "vertexai/gemini-2.5-pro", "label": "Gemini 2.5 Pro", "group": "Vertex AI — Gemini"},
                {"value": "vertexai/gemini-2.5-flash", "label": "Gemini 2.5 Flash (fast)", "group": "Vertex AI — Gemini"},
            ])
        elif secret and secret.has_vertex:
            # VertexAI project+region set but no ADC uploaded — may work
            # with workload identity (GKE) or pre-existing gcloud auth
            options.extend([
                {"value": "vertexai/claude-sonnet-4-6", "label": "Claude Sonnet 4.6 (balanced)", "group": "Vertex AI — Claude"},
                {"value": "vertexai/claude-opus-4-6", "label": "Claude Opus 4.6 (most capable)", "group": "Vertex AI — Claude"},
                {"value": "vertexai/claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5 (fast)", "group": "Vertex AI — Claude"},
                {"value": "vertexai/gemini-2.5-pro", "label": "Gemini 2.5 Pro", "group": "Vertex AI — Gemini"},
                {"value": "vertexai/gemini-2.5-flash", "label": "Gemini 2.5 Flash (fast)", "group": "Vertex AI — Gemini"},
            ])
        # Direct Anthropic API
        if secret and secret.anthropic_api_key_enc:
            options.extend([
                {"value": "anthropic/claude-sonnet-4-6", "label": "Claude Sonnet 4.6", "group": "Anthropic (direct)"},
                {"value": "anthropic/claude-opus-4", "label": "Claude Opus 4", "group": "Anthropic (direct)"},
                {"value": "anthropic/claude-haiku-3.5", "label": "Claude Haiku 3.5 (fast)", "group": "Anthropic (direct)"},
            ])
        # Direct OpenAI API
        if secret and secret.openai_api_key_enc:
            options.extend([
                {"value": "openai/gpt-4o", "label": "GPT-4o", "group": "OpenAI"},
                {"value": "openai/o3", "label": "o3 (reasoning)", "group": "OpenAI"},
            ])
        # Direct Gemini API (Google AI Studio)
        if secret and secret.google_api_key_enc:
            options.extend([
                {"value": "gemini/gemini-2.5-flash", "label": "Gemini 2.5 Flash", "group": "Gemini (AI Studio)"},
                {"value": "gemini/gemini-2.5-pro", "label": "Gemini 2.5 Pro", "group": "Gemini (AI Studio)"},
            ])
        return options

    def get_default_model(self, has_adc: bool, has_gemini: bool) -> str:
        # VertexAI is the primary provider for the target audience
        if has_adc:
            return "vertexai/claude-sonnet-4-6"
        if has_gemini:
            return "gemini/gemini-2.5-flash"
        return ""  # Crush will prompt the user

    def exec_model_update(self, pod_name: str, namespace: str, model: str) -> None:
        # Crush doesn't have an exec-based model update mechanism like opencode.
        # Model is set at launch time via config. For running pods, this is a no-op.
        pass

    def get_env_from_sources(self) -> list:
        from kubernetes import client
        return [
            client.V1EnvFromSource(
                secret_ref=client.V1SecretEnvSource(
                    name="crush-secret", optional=True
                )
            )
        ]

    def get_extra_env(self, has_adc: bool) -> list:
        from kubernetes import client
        env = []
        if has_adc:
            env.append(client.V1EnvVar(
                name="GOOGLE_APPLICATION_CREDENTIALS",
                value="/app/gcloud/credentials.json",
            ))
        return env

    def get_extra_volumes(self, has_adc: bool) -> list:
        from kubernetes import client
        volumes = []
        if has_adc:
            volumes.append(
                client.V1Volume(
                    name="gcloud-creds",
                    secret=client.V1SecretVolumeSource(
                        secret_name="crush-secret",
                        items=[
                            client.V1KeyToPath(
                                key="application_default_credentials.json",
                                path="credentials.json",
                            )
                        ],
                    ),
                )
            )
        return volumes

    def get_extra_volume_mounts(self, has_adc: bool) -> list:
        from kubernetes import client
        mounts = []
        if has_adc:
            mounts.append(
                client.V1VolumeMount(
                    name="gcloud-creds",
                    mount_path="/app/gcloud",
                    read_only=True,
                )
            )
        return mounts

    def build_k8s_secret_data(self, secret) -> dict[str, str]:
        import base64
        def _b64(v: str) -> str:
            return base64.b64encode(v.encode()).decode()

        data = {}
        # Crush uses different env var names than OpenCode
        if secret.google_api_key_enc:
            data["GEMINI_API_KEY"] = _b64(secret.google_api_key)
        if hasattr(secret, 'anthropic_api_key_enc') and secret.anthropic_api_key_enc:
            data["ANTHROPIC_API_KEY"] = _b64(secret.anthropic_api_key)
        if hasattr(secret, 'openai_api_key_enc') and secret.openai_api_key_enc:
            data["OPENAI_API_KEY"] = _b64(secret.openai_api_key)
        if secret.google_cloud_project:
            data["VERTEXAI_PROJECT"] = _b64(secret.google_cloud_project)
        if secret.vertex_location:
            data["VERTEXAI_LOCATION"] = _b64(secret.vertex_location)
        if secret.has_adc:
            data["application_default_credentials.json"] = _b64(
                secret.application_default_credentials
            )
        return data
```

### Step 4.4 — Register CrushStrategy

**File:** `swarmer/agent_tools/registry.py` — update `_init()`:

```python
def _init() -> None:
    from swarmer.agent_tools.opencode import OpenCodeStrategy
    from swarmer.agent_tools.crush import CrushStrategy
    register(OpenCodeStrategy())
    register(CrushStrategy())
```

### Step 4.5 — Update `k8s.py` for Crush ConfigMap

When a workspace is created, we now need to create ConfigMaps for **all** registered tools (not just OpenCode). Update the workspace creation flow:

**File:** `swarmer/routers/workspaces.py` — wherever `k8s.apply_opencode_config()` is called on workspace creation, replace with:

```python
from swarmer.agent_tools.registry import all_tools
for tool in all_tools():
    k8s.apply_agent_config(ws.namespace, secret=None, agent_tool=tool.name)
```

### Step 4.6 — Update `chat_proxy.py` for Crush server mode

Crush's server mode exposes a REST API, not a web UI. The chat proxy needs to know the target port from the strategy:

```python
# In _session_ok(): remove the mode == "server" check since both tools support it
# In _acquire_portforward(): accept port parameter
# In _proxy_root(): get port from strategy

from swarmer.agent_tools.registry import get as get_tool
tool = get_tool(session.agent_tool)
port = tool.get_server_port()
local_port = await _acquire_portforward(session.id, session.pod_name, ws_obj.namespace, port)
```

### Step 4.7 — Build and test the Crush container image

```sh
make image-build-crush
make kind-load-crush
```

### Step 4.8 — Verification

```sh
make lint
make dev
# Verify:
# 1. Session create form shows "Crush" in agent tool dropdown
# 2. Selecting "Crush" with VertexAI creds shows Vertex AI models first (Claude + Gemini on Vertex)
# 3. Selecting "Crush" with Anthropic key shows direct Anthropic models in separate group
# 4. Create a Crush session in prompt mode with VertexAI credentials configured
# 5. Launch → pod uses crush:latest image, runs `crush run --yolo --model vertexai/claude-sonnet-4-6 ...`
# 6. Pod env has VERTEXAI_PROJECT + VERTEXAI_LOCATION (not GOOGLE_CLOUD_PROJECT)
# 7. Pod logs show crush output
# 8. Crush TUI mode: launch → xterm.js connects → can run `crush` interactively
```

**Gate:** Crush sessions launch in prompt and TUI modes.

---

## Phase 5 — End-to-End Validation

**Goal:** Verify all three Crush session modes produce expected behavior. Document known limitations.

### Step 5.1 — Prompt mode validation (VertexAI + Anthropic direct)

**Scenario A — VertexAI (primary, shared credentials):**

```
1. Create workspace "crush-vertex-test"
2. Configure GCP Project, Region, and upload ADC JSON in Secrets → API Credentials
3. Create session: tool=Crush, mode=Prompt, model=vertexai/claude-sonnet-4-6
4. Set instruction prompt: "List the files in the current directory and describe what you see"
5. Add a git repo (e.g. a small public repo)
6. Launch
7. Expected: Pod starts, crush runs the prompt via VertexAI, output appears, pod exits (succeeded)
```

Verify:
- [ ] Pod image is `crush:latest`
- [ ] Pod env contains `VERTEXAI_PROJECT` and `VERTEXAI_LOCATION` from `crush-secret`
- [ ] Pod has `GOOGLE_APPLICATION_CREDENTIALS=/app/gcloud/credentials.json`
- [ ] ADC JSON is mounted from `crush-secret` (not `opencode-secret`)
- [ ] Pod command includes `crush run --yolo --model vertexai/claude-sonnet-4-6`
- [ ] Git repo was cloned by init container
- [ ] Output is captured in session.last_output
- [ ] Phase transitions: idle → pending → running → succeeded

**Scenario B — Direct Anthropic API (alternative):**

```
1. Same workspace, configure Anthropic API key in Secrets
2. Create session: tool=Crush, mode=Prompt, model=anthropic/claude-sonnet-4-6
3. Launch
4. Expected: Pod uses ANTHROPIC_API_KEY env var, runs prompt successfully
```

Verify:
- [ ] Pod env contains `ANTHROPIC_API_KEY` from `crush-secret`
- [ ] Pod command includes `crush run --yolo --model anthropic/claude-sonnet-4-6`
- [ ] No VertexAI env vars needed for this path

**Scenario C — OpenCode with same VertexAI credentials:**

```
1. Same workspace (VertexAI creds already configured)
2. Create session: tool=OpenCode, mode=Prompt, model=google-vertex-anthropic/claude-sonnet-4-6@default
3. Launch
4. Expected: Pod uses GOOGLE_CLOUD_PROJECT + VERTEX_LOCATION from opencode-secret
```

Verify:
- [ ] opencode-secret has `GOOGLE_CLOUD_PROJECT` (not `VERTEXAI_PROJECT`)
- [ ] Both secrets were created from the same stored credentials
- [ ] Both sessions succeed with their respective provider configurations

### Step 5.2 — TUI mode validation

```
1. Same workspace, new session: tool=Crush, mode=TUI
2. Launch
3. Expected: Pod starts with `sleep infinity`, xterm.js connects
4. In the terminal: run `crush` (interactive TUI), verify it starts
5. Run `crush run "what is 2+2"` — verify output
```

Verify:
- [ ] Terminal connects via WebSocket
- [ ] `crush` binary is available in PATH
- [ ] Crush can reach the configured API provider
- [ ] `crush.json` config is mounted at `/root/.config/crush/`

### Step 5.3 — Server mode validation

```
1. Same workspace, new session: tool=Crush, mode=Server
2. Launch
3. Expected: Pod runs `crush server --host tcp://0.0.0.0:4096`
4. "Open Web Chat" button behavior: Crush server is REST API, not a web UI
```

**Known limitation:** Crush server mode exposes a REST API + SSE, not an HTML web UI like OpenCode. The "Open Web Chat" link will show the raw API, not a chat interface. Options:

- **Option A (recommended for Phase 5):** Hide the "Open Web Chat" button for Crush server mode. Users interact via TUI mode instead.
- **Option B (future):** Build a thin chat UI in Swarmer that calls the Crush REST API.

Implement Option A in `sessions/detail.html`:

```html
{% if session.mode == 'server' and session.agent_tool != 'crush' %}
<a href="..." class="btn btn-info btn-sm ...">Open Web Chat ↗</a>
{% endif %}
```

Verify:
- [ ] Pod runs `crush server` command
- [ ] K8s Service created with correct port
- [ ] Port-forward connects to the crush server
- [ ] `GET /v1/health` returns 200 through the proxy
- [ ] Chat button is hidden for Crush sessions

### Step 5.4 — Resume/persist validation

```
1. Create Crush prompt session with persist=true, resume=true
2. Launch → runs prompt → succeeds
3. Launch again
4. Expected: --continue flag is passed, PVC is reused
```

### Step 5.5 — Cross-tool isolation validation

```
1. In the same workspace, have both an OpenCode session and a Crush session
2. Launch both
3. Verify each uses its own:
   - Container image (opencode-golang:latest vs crush:latest)
   - ConfigMap (opencode-config vs crush-config)
   - K8s Secret env source (opencode-secret vs crush-secret)
   - Container name (opencode vs crush)
4. Verify they don't interfere with each other
```

### Step 5.6 — Document known limitations

Create or update documentation:

| Limitation | Workaround |
|-----------|------------|
| Crush server mode has no web UI | Use TUI mode for interactive work |
| Crush model update on running pod is a no-op | Model is set at launch; stop and relaunch to change |
| No official Crush container image | Built from GitHub releases; pin version in Makefile |
| Crush `--yolo` skips permission prompts | Acceptable for containerized ephemeral pods |

### Step 5.7 — Update AGENTS.md

Add Crush to the domain model description, update the architecture section to mention the `AgentToolStrategy` pattern, add the new files to the project structure listing, and document the new Makefile targets.

### Step 5.8 — Final Verification

```sh
make lint
make dev
# Full regression:
# 1. OpenCode prompt mode — launch with VertexAI model, verify output, stop
# 2. OpenCode server mode — launch, open web chat, stop
# 3. OpenCode TUI mode — launch, connect terminal, stop
# 4. Crush prompt mode (VertexAI) — launch with vertexai/claude-sonnet-4-6, verify output, stop
# 5. Crush prompt mode (Anthropic direct) — launch with anthropic/claude-sonnet-4-6, verify output, stop
# 6. Crush TUI mode — launch, connect terminal, run crush, stop
# 7. Crush server mode — launch, verify hidden chat button, stop
# 8. Cross-tool: both sessions in same workspace, both running simultaneously
# 9. Secrets: save Vertex credentials, verify both opencode-secret (GOOGLE_CLOUD_PROJECT)
#    and crush-secret (VERTEXAI_PROJECT) exist with correct env var names
# 10. Model selector: verify Crush shows VertexAI models first, then Anthropic/OpenAI/Gemini
# 11. Model selector: verify OpenCode still shows its existing Claude/Gemini model lists
# 12. Persist/resume: verify PVC reuse and --continue flag
# 13. ADC mount: verify credentials.json mounted from correct tool-specific secret
```

---

## File Change Summary

### New files (8)

| File | Phase |
|------|-------|
| `swarmer/agent_tools/__init__.py` | 1 |
| `swarmer/agent_tools/opencode.py` | 1 |
| `swarmer/agent_tools/registry.py` | 1 |
| `swarmer/agent_tools/crush.py` | 4 |
| `Containerfile.crush` | 4 |

### Modified files (14)

| File | Phase(s) | Nature of change |
|------|----------|-----------------|
| `swarmer/config.py` | 2 | Replace `agent_image` with per-tool settings |
| `swarmer/database.py` | 2, 3 | Add migrations for `agent_tool`, `anthropic_api_key_enc`, `openai_api_key_enc` |
| `swarmer/models/session.py` | 2 | Add `agent_tool` column + `AGENT_TOOLS` constant |
| `swarmer/models/opencode_secret.py` | 3 | Add `anthropic_api_key_enc`, `openai_api_key_enc` + accessors |
| `swarmer/k8s.py` | 1, 2, 3 | Generalize config/secret/model helpers |
| `swarmer/k8s_session.py` | 1, 2, 4 | Delegate to strategy in `build_session_pod()` + parameterize `create_session_service()` |
| `swarmer/routers/sessions.py` | 2 | Accept `agent_tool` in create/edit, delegate model options to strategy |
| `swarmer/routers/secrets.py` | 3 | Accept new credential fields, rename tab, sync all tool secrets |
| `swarmer/routers/workspaces.py` | 4 | Create ConfigMaps for all tools on workspace creation |
| `swarmer/routers/chat_proxy.py` | 4 | Parameterize port in port-forward |
| `swarmer/templates/sessions/new.html` | 2 | Add agent tool selector, de-hardcode OpenCode text |
| `swarmer/templates/sessions/list.html` | 2 | Add Tool column |
| `swarmer/templates/sessions/detail.html` | 2, 5 | Add agent tool selector, hide chat button for Crush |
| `swarmer/templates/secrets/tabs.html` | 3 | Rename tab, add Anthropic/OpenAI fields |
| `swarmer/templates/workspaces/detail.html` | 2 | Add Tool column |
| `.env.example` | 2 | Add per-tool image vars |
| `Makefile` | 4 | Add `image-build-crush`, `kind-load-crush` targets |
| `AGENTS.md` | 5 | Document new architecture |

### Unchanged files

| File | Reason |
|------|--------|
| `swarmer/models/__init__.py` | No new models (extends existing `OpencodeSecret`) |
| `swarmer/models/workspace.py` | No changes needed |
| `swarmer/models/github_pat.py` | Shared across tools |
| `swarmer/models/session_repo.py` | Shared across tools |
| `swarmer/routers/auth.py` | No tool awareness needed |
| `swarmer/routers/tui_ws.py` | Uses kubectl exec — tool-agnostic |
| `swarmer/main.py` | No new routers needed |
| `swarmer/auth.py` | No changes |
| `swarmer/crypto.py` | No changes |
| `swarmer/deps.py` | No changes |
| `swarmer/flash.py` | No changes |
| `Containerfile` | Swarmer's own image — no changes |

---

## Risk Mitigation Checkpoints

| After Phase | Checkpoint |
|-------------|-----------|
| 1 | `build_session_pod()` produces byte-identical pod specs as before (compare YAML output) |
| 2 | All existing sessions have `agent_tool='opencode'` after migration |
| 3 | Saving credentials with only Google/Vertex fields still produces a valid `opencode-secret` AND `crush-secret` with correct env var names (`GOOGLE_CLOUD_PROJECT` vs `VERTEXAI_PROJECT`) |
| 3 | ADC JSON appears in both `opencode-secret` and `crush-secret` under `application_default_credentials.json` key |
| 4 | Crush container image builds and `crush --version` runs inside it |
| 5 | Both tools can run simultaneously in the same workspace namespace, each reading from their own K8s Secret |
| 5 | A Crush session with VertexAI model `vertexai/claude-sonnet-4-6` succeeds when GCP project + region + ADC are configured |
