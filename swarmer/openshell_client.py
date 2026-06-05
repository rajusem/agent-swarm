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
    """Collect credentials from DB models into an env-var dict.

    The OpenShell SDK injects credentials via SandboxSpec.environment, not
    via a separate Provider object. This function returns a plain dict that
    create_sandbox passes into the spec.
    """
    env_vars: dict[str, str] = {}

    if workspace_secret:
        if getattr(workspace_secret, "google_api_key", None):
            env_vars["GOOGLE_API_KEY"] = workspace_secret.google_api_key
        if getattr(workspace_secret, "anthropic_api_key", None):
            env_vars["ANTHROPIC_API_KEY"] = workspace_secret.anthropic_api_key
        if getattr(workspace_secret, "google_cloud_project", None):
            env_vars["GOOGLE_CLOUD_PROJECT"] = workspace_secret.google_cloud_project

    if github_pat:
        env_vars["GITHUB_PAT"] = github_pat.token
        if getattr(github_pat, "username", None):
            env_vars["GITHUB_USERNAME"] = github_pat.username

    for mcp in (mcp_servers or []):
        if getattr(mcp, "catalog_key", None) == "jira":
            config = getattr(mcp, "config", {}) or {}
            for k, v in config.items():
                env_vars[k] = str(v)

    return env_vars


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
    policy_yaml: str,
    client=None,
):
    """Create an OpenShell sandbox and wait for it to be ready.

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

    def _do_create():
        return client.create(spec=spec)

    ref = await asyncio.to_thread(_do_create)
    await asyncio.to_thread(client.wait_ready, ref.name)
    return ref


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

        def _do_clone(s=sid, t=target, r=repo):
            client.exec(s, ["git", "clone", r.url, t])

        await asyncio.to_thread(_do_clone)


async def write_agent_config(
    sandbox_name: str,
    tool_name: str,
    config_json: str,
    client=None,
) -> None:
    """Write agent config JSON to /sandbox/.config/{tool_name}/."""
    if client is None:
        client = _get_client()
    sid = await _sandbox_id(sandbox_name, client)
    config_dir = f"/sandbox/.config/{tool_name}"
    config_path = f"{config_dir}/{tool_name}.json"
    script = f"mkdir -p {config_dir} && cat > {config_path} << 'EOCFG'\n{config_json}\nEOCFG"

    def _do_write(s=sid):
        client.exec(s, ["sh", "-c", script])

    await asyncio.to_thread(_do_write)


async def write_agents_md(sandbox_name: str, content: str, client=None) -> None:
    """Write content to /sandbox/AGENTS.md."""
    if client is None:
        client = _get_client()
    sid = await _sandbox_id(sandbox_name, client)
    script = f"cat > /sandbox/AGENTS.md << 'EOMD'\n{content}\nEOMD"

    def _do_write(s=sid):
        client.exec(s, ["sh", "-c", script])

    await asyncio.to_thread(_do_write)


async def write_file(sandbox_name: str, path: str, content: str, client=None) -> None:
    """Write arbitrary content to a file path inside the sandbox."""
    if client is None:
        client = _get_client()
    sid = await _sandbox_id(sandbox_name, client)
    parent = shlex.quote(str(pathlib.Path(path).parent))
    dest = shlex.quote(path)
    script = f"mkdir -p {parent} && cat > {dest} << 'EOFILE'\n{content}\nEOFILE"

    def _do_write(s=sid):
        client.exec(s, ["sh", "-c", script])

    await asyncio.to_thread(_do_write)


async def start_agent(sandbox_name: str, cmd: list[str], client=None) -> None:
    """Start the agent process inside the sandbox via exec."""
    if client is None:
        client = _get_client()
    sid = await _sandbox_id(sandbox_name, client)

    def _do_start(s=sid):
        client.exec(s, cmd)

    await asyncio.to_thread(_do_start)


async def exec_command(sandbox_name: str, cmd: list[str], client) -> Any:
    """Execute a command inside the sandbox; returns ExecResult (.stdout, .stderr, .exit_code)."""
    sid = await _sandbox_id(sandbox_name, client)

    def _do_exec(s=sid):
        return client.exec(s, cmd)

    return await asyncio.to_thread(_do_exec)
