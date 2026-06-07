"""
OpenShell sandbox client wrapper for AgentSwarm session lifecycle.

Wraps the synchronous OpenShell gRPC SDK (pip install openshell) and exposes
async helpers by running blocking calls via asyncio.to_thread().

Credential injection: env vars go directly into SandboxSpec.environment —
there is no provider_create RPC in this SDK version.
"""
from __future__ import annotations

import asyncio
import logging
import pathlib
import queue
import shlex
from typing import Any

log = logging.getLogger(__name__)


def _get_client():
    """Internal factory — reads settings and returns a configured SandboxClient."""
    from openshell import SandboxClient, TlsConfig  # noqa: F401 (optional dep)
    from swarmer.config import settings

    tls = None
    if settings.openshell_tls_ca:
        tls = TlsConfig(
            ca_path=pathlib.Path(settings.openshell_tls_ca),
            cert_path=pathlib.Path(settings.openshell_tls_cert),
            key_path=pathlib.Path(settings.openshell_tls_key),
        )
    # When mTLS is configured, use certificate auth for gateway admin operations
    # (provider create/update, sandbox create). Bearer tokens in 0.0.55+ use a
    # sandbox-scoped format that doesn't authorize admin RPCs.
    bearer = None if tls else (settings.openshell_bearer_token or None)
    return SandboxClient(
        settings.openshell_gateway_url,
        tls=tls,
        bearer_token=bearer,
    )


def get_client(
    gateway_url: str,
    tls_ca_path: str | None = None,
    tls_cert_path: str | None = None,
    tls_key_path: str | None = None,
):
    """Public factory for e2e tests and direct usage."""
    from openshell import SandboxClient, TlsConfig  # noqa: F401 (optional dep)

    tls = None
    if tls_ca_path:
        tls = TlsConfig(
            ca_path=pathlib.Path(tls_ca_path),
            cert_path=pathlib.Path(tls_cert_path),
            key_path=pathlib.Path(tls_key_path),
        )
    return SandboxClient(gateway_url, tls=tls)


async def create_provider(
    session,
    workspace_secret,
    github_pat,
    mcp_servers: list,
    client=None,
) -> dict[str, str]:
    """Return non-credential env vars for the sandbox (MCP Jira config only).

    AI credentials and GitHub tokens are injected by the OpenShell gateway
    via the Provider API (ensure_provider / attach_sandbox_provider).
    """
    env_vars: dict[str, str] = {}
    for mcp in (mcp_servers or []):
        if getattr(mcp, "catalog_key", None) == "jira":
            config = getattr(mcp, "config", {}) or {}
            for k, v in config.items():
                env_vars[k] = str(v)
    return env_vars


async def ensure_provider(
    name: str,
    profile_type: str,
    config: dict[str, str],
    credentials: dict[str, str] | None = None,
    client=None,
) -> None:
    """Create or update a named provider on the gateway with its credentials.

    For static API keys the gateway stores credentials securely and injects
    them as env vars into the sandbox via GetSandboxProviderEnvironment.
    Credentials are stored server-side (returned as REDACTED); use UpdateProvider
    on subsequent launches to rotate keys.
    """
    from openshell._proto import openshell_pb2
    import grpc

    if client is None:
        client = _get_client()

    def _build_provider(req_provider):
        req_provider.metadata.name = name
        req_provider.type = profile_type
        for k, v in (config or {}).items():
            req_provider.config[k] = v
        for k, v in (credentials or {}).items():
            req_provider.credentials[k] = v

    def _do_ensure():
        create_req = openshell_pb2.CreateProviderRequest()
        _build_provider(create_req.provider)
        try:
            client._stub.CreateProvider(create_req, timeout=client._timeout)
        except grpc.RpcError as exc:
            if isinstance(exc, grpc.Call) and exc.code() == grpc.StatusCode.ALREADY_EXISTS:
                update_req = openshell_pb2.UpdateProviderRequest()
                _build_provider(update_req.provider)
                client._stub.UpdateProvider(update_req, timeout=client._timeout)
            else:
                raise

    await asyncio.to_thread(_do_ensure)


async def delete_provider(name: str, client=None) -> None:
    """Delete a named provider from the gateway, ignoring NOT_FOUND errors."""
    from openshell._proto import openshell_pb2
    import grpc

    if client is None:
        client = _get_client()

    def _do_delete():
        req = openshell_pb2.DeleteProviderRequest()
        req.name = name
        try:
            client._stub.DeleteProvider(req, timeout=client._timeout)
        except grpc.RpcError as exc:
            if isinstance(exc, grpc.Call) and exc.code() == grpc.StatusCode.NOT_FOUND:
                return
            raise

    await asyncio.to_thread(_do_delete)


async def create_vertex_provider(
    name: str,
    project: str,
    location: str,
    client=None,
) -> None:
    """Delete any existing provider with this name and create a fresh one with
    exactly the structure the google-vertex-ai profile requires:
      config:      VERTEX_AI_PROJECT_ID, VERTEX_AI_REGION
      credentials: GOOGLE_VERTEX_AI_TOKEN  (placeholder; refreshed by gateway)
    """
    await delete_provider(name, client=client)
    await ensure_provider(
        name, "google-vertex-ai",
        config={"VERTEX_AI_PROJECT_ID": project, "VERTEX_AI_REGION": location},
        credentials={"GOOGLE_VERTEX_AI_TOKEN": "__placeholder__"},
        client=client,
    )


async def configure_provider_credential(
    provider_name: str,
    credential_key: str,
    credential_value: str,
    client=None,
) -> None:
    """Store a static credential on a gateway-managed provider."""
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    def _do_configure():
        req = openshell_pb2.ConfigureProviderRefreshRequest()
        req.provider = provider_name
        req.credential_key = credential_key
        req.strategy = openshell_pb2.PROVIDER_CREDENTIAL_REFRESH_STRATEGY_STATIC
        req.material["value"] = credential_value
        req.secret_material_keys.append("value")
        client._stub.ConfigureProviderRefresh(req, timeout=client._timeout)

    await asyncio.to_thread(_do_configure)


async def configure_vertex_provider(
    provider_name: str,
    adc_json: str,
    project: str,
    location: str,
    client=None,
) -> None:
    """Configure a google-vertex-ai provider with auto-refreshing credentials.

    Parses the ADC JSON to choose the appropriate gateway refresh strategy:
    - service_account → GOOGLE_SERVICE_ACCOUNT_JWT (gateway signs JWTs from SA key)
    - authorized_user → OAUTH2_REFRESH_TOKEN (gateway exchanges refresh token)

    Also stores project/location as non-secret config on the provider so the
    gateway injects them as env vars (GOOGLE_CLOUD_PROJECT, VERTEXAI_PROJECT,
    VERTEX_LOCATION, VERTEXAI_LOCATION) alongside the access token.
    """
    import json as _json
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    adc = _json.loads(adc_json)
    adc_type = adc.get("type", "")

    def _do_configure():
        req = openshell_pb2.ConfigureProviderRefreshRequest()
        req.provider = provider_name

        if adc_type == "service_account":
            # The google-vertex-ai profile's credential key is GOOGLE_VERTEX_AI_TOKEN.
            # Gateway refreshes via GOOGLE_SERVICE_ACCOUNT_JWT and injects the resulting
            # token as the GOOGLE_VERTEX_AI_TOKEN env var inside the sandbox.
            req.credential_key = "GOOGLE_VERTEX_AI_TOKEN"
            req.strategy = openshell_pb2.PROVIDER_CREDENTIAL_REFRESH_STRATEGY_GOOGLE_SERVICE_ACCOUNT_JWT
            req.material["client_email"] = adc.get("client_email", "")
            req.material["private_key"] = adc.get("private_key", "")
            req.secret_material_keys.append("private_key")
        elif adc_type == "authorized_user":
            # The google-vertex-ai profile's credential key is GOOGLE_VERTEX_AI_TOKEN.
            # Gateway refreshes via OAUTH2_REFRESH_TOKEN (token_url and scopes are
            # defined in the built-in profile, not in the material).
            req.credential_key = "GOOGLE_VERTEX_AI_TOKEN"
            req.strategy = openshell_pb2.PROVIDER_CREDENTIAL_REFRESH_STRATEGY_OAUTH2_REFRESH_TOKEN
            req.material["client_id"] = adc.get("client_id", "")
            req.material["client_secret"] = adc.get("client_secret", "")
            req.material["refresh_token"] = adc.get("refresh_token", "")
            req.secret_material_keys.extend(["client_secret", "refresh_token"])
        else:
            raise ValueError(f"Unsupported ADC type for VertexAI provider: {adc_type!r}")

        client._stub.ConfigureProviderRefresh(req, timeout=client._timeout)

    await asyncio.to_thread(_do_configure)


async def set_cluster_inference(
    provider_name: str,
    model_id: str,
    *,
    no_verify: bool = False,
    client=None,
) -> None:
    """Configure the cluster-level inference proxy (inference.local) to use a provider.

    Required before agents can use ANTHROPIC_BASE_URL=https://inference.local/v1.
    The gateway routes requests through the provider's credentials (VertexAI tokens).
    Use no_verify=True when the 'global' region causes endpoint verification to fail.
    """
    from openshell._proto import inference_pb2, inference_pb2_grpc

    if client is None:
        client = _get_client()

    def _do_set():
        stub = inference_pb2_grpc.InferenceStub(client._channel)
        req = inference_pb2.SetClusterInferenceRequest(
            provider_name=provider_name,
            model_id=model_id,
            no_verify=no_verify,
        )
        stub.SetClusterInference(req, timeout=client._timeout)

    await asyncio.to_thread(_do_set)


async def enable_providers_v2(client=None) -> None:
    """Enable the providers_v2 feature flag on the gateway (required for google-vertex-ai inference).

    Equivalent to: openshell settings set --global --key providers_v2_enabled --value true
    """
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    def _do_enable():
        req = openshell_pb2.UpdateConfigRequest()
        req.setting_key = "providers_v2_enabled"
        req.setting_value.bool_value = True
        setattr(req, "global", True)
        client._stub.UpdateConfig(req, timeout=client._timeout)

    await asyncio.to_thread(_do_enable)


async def attach_sandbox_provider(sandbox_name: str, provider_name: str, client=None) -> None:
    """Attach a pre-configured gateway provider to a sandbox."""
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    def _do_attach():
        req = openshell_pb2.AttachSandboxProviderRequest()
        req.sandbox_name = sandbox_name
        req.provider_name = provider_name
        client._stub.AttachSandboxProvider(req, timeout=client._timeout)

    await asyncio.to_thread(_do_attach)


async def approve_draft_policy_chunks(
    sandbox_name: str,
    expected_hosts: set[str] | None = None,
    client=None,
) -> list[str]:
    """Approve pending draft policy chunks for known/expected endpoints only.

    The supervisor observes denied network connections and proposes policy rules
    (draft chunks). This function approves only chunks whose endpoints match
    ``expected_hosts`` — arbitrary/unexpected endpoints are left pending and
    logged as warnings for human review.

    Returns a list of unexpected host names that were NOT approved.
    """
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    def _host_matches(host: str, expected: set[str]) -> bool:
        """True if ``host`` matches any pattern in ``expected`` (supports leading *)."""
        for pattern in expected:
            if pattern.startswith("*."):
                if host.endswith(pattern[1:]) or host == pattern[2:]:
                    return True
            elif host == pattern:
                return True
        return False

    def _do_approve():
        try:
            dp = client._stub.GetDraftPolicy(
                openshell_pb2.GetDraftPolicyRequest(name=sandbox_name), timeout=10
            )
            pending = [c for c in dp.chunks if c.status == "pending"]
            if not pending:
                return 0, []

            if expected_hosts is None:
                # No filter: approve all (fallback/permissive mode)
                to_approve = pending
                unexpected = []
            else:
                to_approve = []
                unexpected = []
                for chunk in pending:
                    chunk_hosts = [ep.host for ep in chunk.proposed_rule.endpoints]
                    if all(_host_matches(h, expected_hosts) for h in chunk_hosts):
                        to_approve.append(chunk)
                    else:
                        unexpected.extend(
                            h for h in chunk_hosts if not _host_matches(h, expected_hosts)
                        )

            if unexpected:
                log.warning(
                    "sandbox %s: %d unexpected endpoint(s) not approved: %s",
                    sandbox_name, len(unexpected), unexpected
                )

            if not to_approve:
                return 0, unexpected

            # Approve individual chunks that match expectations
            approved_count = 0
            for chunk in to_approve:
                try:
                    client._stub.ApproveDraftChunk(
                        openshell_pb2.ApproveDraftChunkRequest(
                            name=sandbox_name, chunk_id=chunk.id
                        ), timeout=10
                    )
                    approved_count += 1
                except Exception as exc:
                    log.warning("Failed to approve chunk %s: %s", chunk.rule_name, exc)

            return approved_count, unexpected
        except Exception as exc:
            log.warning("approve_draft_policy_chunks failed for %s: %s", sandbox_name, exc)
            return 0, []

    approved, unexpected = await asyncio.to_thread(_do_approve)
    if approved:
        log.info("sandbox %s: approved %d draft policy chunk(s)", sandbox_name, approved)
        await asyncio.sleep(2)  # let supervisor apply the new policy
    return unexpected


async def import_provider_profiles(profiles: list[dict], client=None) -> None:
    """Import custom provider type profiles into the gateway (idempotent)."""
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    def _do_import():
        req = openshell_pb2.ImportProviderProfilesRequest()
        for p in profiles:
            profile = openshell_pb2.ProviderProfile(
                id=p["id"],
                display_name=p.get("display_name", p["id"]),
                category=p.get("category", openshell_pb2.PROVIDER_PROFILE_CATEGORY_INFERENCE),
                inference_capable=p.get("inference_capable", True),
            )
            for cred in p.get("credentials", []):
                c = openshell_pb2.ProviderProfileCredential(
                    name=cred["name"],
                    required=cred.get("required", True),
                    auth_style=cred.get("auth_style", ""),
                    header_name=cred.get("header_name", ""),
                    query_param=cred.get("query_param", ""),
                )
                for ev in cred.get("env_vars", []):
                    c.env_vars.append(ev)
                refresh = cred.get("refresh")
                if refresh:
                    c.refresh.token_url = refresh.get("token_url", "")
                    for sc in refresh.get("scopes", []):
                        c.refresh.scopes.append(sc)
                    for mat in refresh.get("material", []):
                        m = c.refresh.material.add()
                        m.name = mat["name"]
                        m.required = mat.get("required", True)
                        m.secret = mat.get("secret", False)
                profile.credentials.append(c)
            req.profiles.append(openshell_pb2.ProviderProfileImportItem(profile=profile, source="swarmer"))
        client._stub.ImportProviderProfiles(req, timeout=client._timeout)

    await asyncio.to_thread(_do_import)


async def create_provider_from_env(
    google_api_key: str,
    anthropic_api_key: str,
    github_pat: str,
    client=None,
) -> dict[str, str]:
    """Build env-var dict from explicit credential values (used by e2e tests)."""
    env_vars: dict[str, str] = {}
    if google_api_key:
        env_vars["GOOGLE_API_KEY"] = google_api_key
    if anthropic_api_key:
        env_vars["ANTHROPIC_API_KEY"] = anthropic_api_key
    if github_pat:
        env_vars["GITHUB_PAT"] = github_pat
    return env_vars


async def create_sandbox(
    image: str,
    env_vars: dict[str, str] | None,
    policy,
    provider_names: list[str] | None = None,
    client=None,
):
    """Create an OpenShell sandbox and wait for it to be ready.

    provider_names lists pre-configured gateway providers to attach at creation
    time so the supervisor can call GetSandboxProviderEnvironment at startup
    and receive injected reference tokens before any exec commands run.

    Returns the SandboxRef; caller stores ref.name as session.sandbox_name.
    """
    from openshell._proto import openshell_pb2  # noqa: F401 (optional dep)

    if client is None:
        client = _get_client()

    spec = openshell_pb2.SandboxSpec()
    if image:
        spec.template.image = image
    for k, v in (env_vars or {}).items():
        spec.environment[k] = v
    for pname in (provider_names or []):
        spec.providers.append(pname)
    if policy is not None:
        spec.policy.CopyFrom(policy)

    def _do_create():
        return client.create(spec=spec)

    ref = await asyncio.to_thread(_do_create)
    await _wait_sandbox_ready(ref.name, client=client)
    return ref


async def _wait_sandbox_ready(
    sandbox_name: str,
    client=None,
    timeout: float = 300.0,
    poll_interval: float = 2.0,
) -> None:
    """Wait until the sandbox Ready condition is True.

    The gateway reports readiness via status.conditions[type=Ready, status=True]
    rather than the phase field (which stays UNSPECIFIED=0 in current versions).
    We also wait for the supervisor to become attached so provider env vars are
    available to exec commands.
    """
    import time
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    deadline = time.time() + timeout

    def _poll():
        while time.time() < deadline:
            resp = client._stub.GetSandbox(
                openshell_pb2.GetSandboxRequest(name=sandbox_name), timeout=10
            )
            status = resp.sandbox.status
            if status.phase == openshell_pb2.SANDBOX_PHASE_ERROR:
                raise RuntimeError(f"Sandbox {sandbox_name} entered error phase")
            for cond in status.conditions:
                if cond.type == "Ready" and cond.status == "True":
                    return resp.sandbox
            time.sleep(poll_interval)
        raise RuntimeError(
            f"Sandbox {sandbox_name} not ready after {timeout}s "
            f"(last conditions: {[(c.type, c.status) for c in resp.sandbox.status.conditions]})"
        )

    await asyncio.to_thread(_poll)


async def delete_sandbox(sandbox_name: str, client=None) -> None:
    """Delete an OpenShell sandbox."""
    if client is None:
        client = _get_client()

    def _do_delete():
        client.delete(sandbox_name)

    await asyncio.to_thread(_do_delete)


async def list_sandboxes(client=None) -> list[str]:
    """Return names of all live sandboxes from the OpenShell gateway."""
    if client is None:
        client = _get_client()

    def _do_list():
        return client.list()

    refs = await asyncio.to_thread(_do_list)
    return [ref.name for ref in refs]


async def _sandbox_id(sandbox_name: str, client) -> str:
    """Resolve sandbox name → id (needed by the exec RPC)."""
    ref = await asyncio.to_thread(client.get, sandbox_name)
    return ref.id



async def write_agent_config(
    sandbox_name: str,
    tool_name: str,
    config_json: str,
    client=None,
) -> None:
    """Write agent config JSON to /sandbox/{tool_name}.json (CWD config, read by agent at startup).

    Uses stdin to deliver file content so the gateway's no-newline-in-args
    restriction is never hit.
    """
    if client is None:
        client = _get_client()
    sid = await _sandbox_id(sandbox_name, client)
    
    if tool_name == "crush":
        dest = shlex.quote("/sandbox/.config/crush/crush.json")
        script = f"mkdir -p /sandbox/.config/crush && cat > {dest}"
    else:
        # Write directly to /sandbox/<tool>.json — OpenCode reads config from CWD,
        # and also overwrites the broken LSP config shipped in the container image
        # (missing required 'extensions' field).
        dest = shlex.quote(f"/sandbox/{tool_name}.json")
        script = f"cat > {dest}"

    def _do_write(s=sid):
        client.exec(s, ["sh", "-c", script], stdin=config_json.encode())

    await asyncio.to_thread(_do_write)


async def write_agents_md(sandbox_name: str, content: str, client=None) -> None:
    """Write content to /sandbox/AGENTS.md via stdin (avoids newline-in-arg restriction)."""
    if client is None:
        client = _get_client()
    sid = await _sandbox_id(sandbox_name, client)

    def _do_write(s=sid):
        client.exec(s, ["sh", "-c", "cat > /sandbox/AGENTS.md"], stdin=content.encode())

    await asyncio.to_thread(_do_write)


async def write_file(sandbox_name: str, path: str, content: str, client=None) -> None:
    """Write arbitrary content to a file inside the sandbox via stdin."""
    if client is None:
        client = _get_client()
    sid = await _sandbox_id(sandbox_name, client)
    parent = shlex.quote(str(pathlib.Path(path).parent))
    dest = shlex.quote(path)
    script = f"mkdir -p {parent} && cat > {dest}"

    def _do_write(s=sid):
        client.exec(s, ["sh", "-c", script], stdin=content.encode())

    await asyncio.to_thread(_do_write)


async def start_agent(sandbox_name: str, cmd: list[str], client=None) -> None:
    """Start the agent as a detached background process so exec() returns immediately."""
    if client is None:
        client = _get_client()
    sid = await _sandbox_id(sandbox_name, client)

    shell_cmd = " ".join(shlex.quote(c) for c in cmd)
    bg_cmd = ["sh", "-c", f"nohup {shell_cmd} >/sandbox/.agent.log 2>&1 &"]

    def _do_start(s=sid):
        client.exec(s, bg_cmd)

    await asyncio.to_thread(_do_start)


async def read_opencode_response(sandbox_name: str, client=None) -> str:
    """Read the last assistant message from OpenCode's SQLite DB.

    OpenCode stores conversation history in /sandbox/.opencode/opencode.db rather
    than writing to stdout. This extracts the most recent assistant text parts.
    """

    if client is None:
        client = _get_client()

    # Write the reader script via stdin (no newlines in command arg) then execute it.
    reader = b"""
import sqlite3, json
try:
    conn = sqlite3.connect('/sandbox/.opencode/opencode.db')
    conn.execute('PRAGMA wal_checkpoint(FULL)')
    rows = conn.execute('''
        SELECT p.data FROM part p
        JOIN message m ON p.message_id = m.id
        WHERE json_extract(m.data, '$.role') = 'assistant'
          AND json_extract(p.data, '$.type') = 'text'
        ORDER BY p.time_created ASC
    ''').fetchall()
    texts = [json.loads(r[0]).get('text', '') for r in rows if r[0]]
    out = '\\n'.join(t for t in texts if t.strip())
    print(out[:8000] if out else '', end='')
    conn.close()
except Exception as exc:
    import sys; print(f'DB_ERR:{exc}', file=sys.stderr, end='')
"""

    def _do_read():
        try:
            sid = client.get(sandbox_name).id
            client.exec(sid, ["sh", "-c", "cat > /tmp/_oc_read.py"], stdin=reader, timeout_seconds=10)
            result = client.exec(sid, ["python3", "/tmp/_oc_read.py"], timeout_seconds=15)
            if result.stderr and "DB_ERR:" in result.stderr:
                log.warning("read_opencode_response: %s  sandbox=%s", result.stderr.strip(), sandbox_name)
            return (result.stdout or "").strip()
        except Exception as exc:
            log.warning("read_opencode_response failed for sandbox %s: %s", sandbox_name, exc)
            return ""

    return await asyncio.to_thread(_do_read)


async def exec_command(
    sandbox_name: str,
    cmd: list[str],
    client,
    stdin: bytes | None = None,
    timeout_seconds: int | None = None,
) -> Any:
    """Execute a command inside the sandbox; returns ExecResult (.stdout, .stderr, .exit_code)."""
    if client is None:
        client = _get_client()
    sid = await _sandbox_id(sandbox_name, client)

    def _do_exec(s=sid):
        return client.exec(s, cmd, stdin=stdin, timeout_seconds=timeout_seconds)

    return await asyncio.to_thread(_do_exec)


def exec_interactive(
    sandbox_name: str,
    sandbox_id: str,
    command: list[str],
    cols: int,
    rows: int,
    client=None,
):
    """Open an interactive PTY exec stream for a sandbox (for TUI WebSocket bridge).

    Returns (response_iterator, input_queue) where:
    - response_iterator: gRPC stream of ExecSandboxEvent messages
    - input_queue: thread-safe queue.Queue — put ExecSandboxInput messages, or None to close

    The caller runs a background thread that drains response_iterator and a write loop
    that puts input messages. This function is synchronous; call from a background thread.
    """
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    input_q: queue.Queue = queue.Queue()

    def _request_generator():
        start_msg = openshell_pb2.ExecSandboxInput(
            start=openshell_pb2.ExecSandboxRequest(
                sandbox_id=sandbox_id,
                command=command,
                workdir="/sandbox",
                tty=True,
                cols=cols,
                rows=rows,
            )
        )
        yield start_msg
        while True:
            item = input_q.get()
            if item is None:
                return
            yield item

    response_stream = client._stub.ExecSandboxInteractive(_request_generator())
    return response_stream, input_q


async def expose_service(
    sandbox_name: str,
    service_name: str,
    target_port: int,
    client=None,
) -> str:
    """Expose a sandbox's port via the OpenShell gateway and return the routable URL."""
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    def _do():
        req = openshell_pb2.ExposeServiceRequest(
            sandbox=sandbox_name,
            service=service_name,
            target_port=target_port,
            domain=True,
        )
        resp = client._stub.ExposeService(req, timeout=client._timeout)
        url = resp.url
        log.info("expose_service %s/%s → %s", sandbox_name, service_name, url)
        # The gateway returns the internal cluster port (e.g. 8080) but in local
        # dev with kubectl port-forward the gateway is accessible on a different
        # port (e.g. 17670). Rewrite to the configured gateway port so Swarmer
        # can reach it from the host.
        from swarmer.config import settings
        gw = settings.openshell_gateway_url or ""
        gw_port = gw.rsplit(":", 1)[-1] if ":" in gw else ""
        if gw_port and gw_port.isdigit():
            from urllib.parse import urlparse, urlunparse
            p = urlparse(url)
            if p.port and str(p.port) != gw_port:
                url = urlunparse(p._replace(netloc=f"{p.hostname}:{gw_port}"))
                log.info("expose_service: rewrote port %s → %s", p.port, gw_port)
        return url

    return await asyncio.to_thread(_do)


async def delete_service(
    sandbox_name: str,
    service_name: str,
    client=None,
) -> None:
    """Delete an exposed sandbox service endpoint."""
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    def _do():
        req = openshell_pb2.DeleteServiceRequest(
            sandbox=sandbox_name,
            service=service_name,
        )
        client._stub.DeleteService(req, timeout=client._timeout)

    await asyncio.to_thread(_do)
