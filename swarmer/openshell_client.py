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
    return SandboxClient(
        settings.openshell_gateway_url,
        tls=tls,
        bearer_token=settings.openshell_bearer_token or None,
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


async def clone_repos(sandbox_name: str, repos: list, client=None) -> None:
    """Clone git repos into /sandbox/ via exec (one call per repo)."""
    if client is None:
        client = _get_client()
    sid = await _sandbox_id(sandbox_name, client)
    for repo in repos:
        target = f"/sandbox/{repo.local_path}"
        # SessionRepo uses repo_url; _Repo dataclasses also use repo_url
        url = getattr(repo, "repo_url", None) or getattr(repo, "url", "")

        def _do_clone(s=sid, t=target, u=url):
            client.exec(s, ["git", "clone", u, t])

        await asyncio.to_thread(_do_clone)


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
    # Write directly to /sandbox/<tool>.json — OpenCode/Crush read config from CWD.
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
    """Start the agent process inside the sandbox via exec."""
    if client is None:
        client = _get_client()
    sid = await _sandbox_id(sandbox_name, client)

    def _do_start(s=sid):
        client.exec(s, cmd)

    await asyncio.to_thread(_do_start)


async def read_opencode_response(sandbox_name: str, client=None) -> str:
    """Read the last assistant message from OpenCode's SQLite DB.

    OpenCode stores conversation history in /sandbox/.opencode/opencode.db rather
    than writing to stdout. This extracts the most recent assistant text parts.
    """
    import base64

    if client is None:
        client = _get_client()

    # Use base64-encoded inline script to avoid temp files and newline-in-arg issues
    script = b"""
import sqlite3, json
db = '/sandbox/.opencode/opencode.db'
try:
    conn = sqlite3.connect(db)
    conn.execute('PRAGMA wal_checkpoint(FULL)')
    rows = conn.execute('''
        SELECT p.data FROM part p
        JOIN message m ON p.message_id = m.id
        WHERE json_extract(m.data, "$.role") = "assistant"
          AND json_extract(p.data, "$.type") = "text"
        ORDER BY p.time_created DESC LIMIT 5
    ''').fetchall()
    texts = [json.loads(r[0]).get('text', '') for r in rows if r[0]]
    out = '\\n'.join(t for t in reversed(texts) if t.strip())
    print(out[:8000] if out else '', end='')
    conn.close()
except Exception as exc:
    import sys; print(f'DB_ERR:{exc}', file=sys.stderr, end='')
"""
    b64 = base64.b64encode(script).decode()
    # Decode and run inline — no temp file, no newlines in command argument
    run_cmd = f"python3 -c \"import base64,sys; exec(base64.b64decode('{b64}').decode())\""

    def _do_read():
        try:
            sid = client.get(sandbox_name).id
            result = client.exec(sid, ["sh", "-c", run_cmd], timeout_seconds=15)
            if result.stderr and result.stderr.strip().startswith("DB_ERR:"):
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
