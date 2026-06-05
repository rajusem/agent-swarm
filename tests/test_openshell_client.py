"""
Tests for swarmer.openshell_client — the OpenShell SDK wrapper.

Validates the session lifecycle helpers:
  - create_provider() builds env-var dicts from DB credentials (no K8s Secrets)
  - create_sandbox() calls SandboxClient.create() and wait_ready()
  - exec helpers (clone_repos, write_agent_config, write_agents_md, start_agent)
    use /sandbox/ paths, not /workspace/
  - delete_sandbox() calls SandboxClient.delete() without touching PVCs
"""
import os
import sys

import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# Inject openshell SDK stub so swarmer.openshell_client imports succeed
# without a real installed package.
# ---------------------------------------------------------------------------


class _SandboxTemplate:
    def __init__(self):
        self.image = ""
        self.environment = {}


class _SandboxSpec:
    def __init__(self):
        self.template = _SandboxTemplate()
        self.environment = {}
        self.policy = None


_proto_stub = MagicMock()
_proto_stub.openshell_pb2 = MagicMock()
_proto_stub.openshell_pb2.SandboxSpec = _SandboxSpec

_sdk_stub = MagicMock()
_sdk_stub.SandboxClient = MagicMock
_sdk_stub.TlsConfig = MagicMock
_sdk_stub._proto = _proto_stub

sys.modules.setdefault("openshell", _sdk_stub)
sys.modules.setdefault("openshell._proto", _proto_stub)
sys.modules.setdefault("openshell._proto.openshell_pb2", _proto_stub.openshell_pb2)

import swarmer.openshell_client as oc  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sdk_client():
    """Mock object mimicking the synchronous openshell.SandboxClient interface."""
    client = MagicMock()
    ref = MagicMock()
    ref.name = "sandbox-s42-abc1"
    ref.id = "sandbox-s42-abc1"
    client.create = MagicMock(return_value=ref)
    client.get = MagicMock(return_value=ref)
    client.wait_ready = MagicMock(return_value=ref)
    client.exec = MagicMock(return_value=MagicMock(exit_code=0, stdout=""))
    client.delete = MagicMock(return_value=True)
    return client


@pytest.fixture
def session():
    s = MagicMock()
    s.id = 42
    s.mode = "tui"
    s.agent_tool = "opencode"
    s.model = "google-vertex-anthropic/claude-sonnet-4-6"
    s.instruction_prompt = ""
    s.sandbox_name = None
    repo = MagicMock()
    repo.url = "https://github.com/stolostron/agent-swarm"
    repo.branch = "main"
    repo.local_path = "agent-swarm"
    s.repos = [repo]
    return s


@pytest.fixture
def workspace_secret():
    secret = MagicMock()
    secret.google_api_key = "gkey-test"
    secret.anthropic_api_key = "akey-test"
    secret.google_cloud_project = "my-project"
    return secret


@pytest.fixture
def github_pat():
    pat = MagicMock()
    pat.token = "ghp_testtoken"
    pat.username = "jpacker"
    return pat


# ---------------------------------------------------------------------------
# 1. Provider creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_provider_returns_env_vars(sdk_client, session, workspace_secret):
    env_vars = await oc.create_provider(
        session=session,
        workspace_secret=workspace_secret,
        github_pat=None,
        mcp_servers=[],
    )
    assert isinstance(env_vars, dict)
    assert "GOOGLE_API_KEY" in env_vars
    assert "ANTHROPIC_API_KEY" in env_vars


@pytest.mark.asyncio
async def test_create_provider_does_not_create_k8s_agent_secret(session, workspace_secret):
    from swarmer import k8s
    with patch.object(k8s, "create_session_agent_secret", MagicMock()) as mock_fn:
        await oc.create_provider(
            session=session,
            workspace_secret=workspace_secret,
            github_pat=None,
            mcp_servers=[],
        )
        mock_fn.assert_not_called()


@pytest.mark.asyncio
async def test_create_provider_does_not_create_k8s_pat_secret(session, workspace_secret, github_pat):
    from swarmer import k8s
    with patch.object(k8s, "create_session_pat_secret", MagicMock()) as mock_fn:
        await oc.create_provider(
            session=session,
            workspace_secret=workspace_secret,
            github_pat=github_pat,
            mcp_servers=[],
        )
        mock_fn.assert_not_called()


@pytest.mark.asyncio
async def test_create_provider_includes_github_credentials(session, workspace_secret, github_pat):
    env_vars = await oc.create_provider(
        session=session,
        workspace_secret=workspace_secret,
        github_pat=github_pat,
        mcp_servers=[],
    )
    assert "GITHUB_PAT" in env_vars
    assert env_vars["GITHUB_PAT"] == github_pat.token


@pytest.mark.asyncio
async def test_create_provider_includes_jira_mcp_credentials(session, workspace_secret):
    jira_mcp = MagicMock()
    jira_mcp.catalog_key = "jira"
    jira_mcp.config = {
        "JIRA_SERVER_URL": "https://redhat.atlassian.net",
        "JIRA_ACCESS_TOKEN": "tok-test",
    }
    env_vars = await oc.create_provider(
        session=session,
        workspace_secret=workspace_secret,
        github_pat=None,
        mcp_servers=[jira_mcp],
    )
    assert "JIRA_SERVER_URL" in env_vars
    assert "JIRA_ACCESS_TOKEN" in env_vars


# ---------------------------------------------------------------------------
# 2. Sandbox creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_sandbox_passes_byoc_image(sdk_client):
    image = "quay.io/jpacker/opencode:latest"
    with patch.object(oc, "_get_client", return_value=sdk_client):
        await oc.create_sandbox(
            image=image,
            env_vars={},
            policy_yaml="version: 1\n",
        )
    sdk_client.create.assert_called_once()
    # The spec object is passed as a kwarg; check its template.image was set
    spec = sdk_client.create.call_args.kwargs["spec"]
    assert spec.template.image == image


@pytest.mark.asyncio
async def test_wait_ready_called_after_create(sdk_client):
    with patch.object(oc, "_get_client", return_value=sdk_client):
        await oc.create_sandbox(
            image="quay.io/jpacker/opencode:latest",
            env_vars={},
            policy_yaml="version: 1\n",
        )
    sdk_client.wait_ready.assert_called_once()


@pytest.mark.asyncio
async def test_create_sandbox_does_not_create_pvc(sdk_client):
    from swarmer import k8s_session as k8s_sess
    with patch.object(oc, "_get_client", return_value=sdk_client):
        with patch.object(k8s_sess, "ensure_session_pvc") as mock_pvc:
            await oc.create_sandbox(
                image="quay.io/jpacker/opencode:latest",
                env_vars={},
                policy_yaml="version: 1\n",
            )
            mock_pvc.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Exec operations: git clone, config, AGENTS.md, agent startup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_git_clone_exec_targets_sandbox_path(sdk_client, session):
    sandbox_name = "sandbox-s42-abc1"
    with patch.object(oc, "_get_client", return_value=sdk_client):
        await oc.clone_repos(sandbox_name=sandbox_name, repos=session.repos)
    assert sdk_client.exec.called
    calls_repr = str(sdk_client.exec.call_args_list)
    assert "/sandbox/" in calls_repr
    assert "/workspace/" not in calls_repr


@pytest.mark.asyncio
async def test_git_clone_exec_called_per_repo(sdk_client, session):
    repo2 = MagicMock()
    repo2.url = "https://github.com/stolostron/other-repo"
    repo2.branch = "main"
    repo2.local_path = "other-repo"
    session.repos = [session.repos[0], repo2]
    sandbox_name = "sandbox-s42-abc1"
    with patch.object(oc, "_get_client", return_value=sdk_client):
        await oc.clone_repos(sandbox_name=sandbox_name, repos=session.repos)
    assert sdk_client.exec.call_count == 2


@pytest.mark.asyncio
async def test_config_write_exec_uses_sandbox_config_path(sdk_client):
    sandbox_name = "sandbox-s42-abc1"
    config_json = '{"$schema": "https://opencode.ai/config.json", "mcpServers": {}}'
    with patch.object(oc, "_get_client", return_value=sdk_client):
        await oc.write_agent_config(
            sandbox_name=sandbox_name,
            tool_name="opencode",
            config_json=config_json,
        )
    sdk_client.exec.assert_called_once()
    calls_repr = str(sdk_client.exec.call_args)
    assert "/sandbox/" in calls_repr
    assert "/workspace/" not in calls_repr


@pytest.mark.asyncio
async def test_agents_md_exec_writes_to_sandbox(sdk_client):
    sandbox_name = "sandbox-s42-abc1"
    with patch.object(oc, "_get_client", return_value=sdk_client):
        await oc.write_agents_md(sandbox_name=sandbox_name, content="# Instructions\n\nFix the bug.")
    sdk_client.exec.assert_called_once()
    calls_repr = str(sdk_client.exec.call_args)
    assert "AGENTS.md" in calls_repr


@pytest.mark.asyncio
async def test_start_agent_exec_called_with_agent_cmd(sdk_client):
    sandbox_name = "sandbox-s42-abc1"
    cmd = ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"]
    with patch.object(oc, "_get_client", return_value=sdk_client):
        await oc.start_agent(sandbox_name=sandbox_name, cmd=cmd)
    sdk_client.exec.assert_called_once()
    calls_repr = str(sdk_client.exec.call_args)
    assert "opencode" in calls_repr


# ---------------------------------------------------------------------------
# 4. Session stop / cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_calls_delete_sandbox(sdk_client):
    sandbox_name = "sandbox-s42-abc1"
    with patch.object(oc, "_get_client", return_value=sdk_client):
        await oc.delete_sandbox(sandbox_name=sandbox_name)
    sdk_client.delete.assert_called_once_with(sandbox_name)


@pytest.mark.asyncio
async def test_stop_does_not_call_pvc_delete(sdk_client):
    from swarmer import k8s_session as k8s_sess
    with patch.object(oc, "_get_client", return_value=sdk_client):
        with patch.object(k8s_sess, "delete_session_pvc") as mock_pvc:
            await oc.delete_sandbox(sandbox_name="sandbox-s42-abc1")
            mock_pvc.assert_not_called()


@pytest.mark.asyncio
async def test_stop_does_not_call_cleanup_session_secrets(sdk_client):
    from swarmer import k8s
    with patch.object(oc, "_get_client", return_value=sdk_client):
        with patch.object(k8s, "cleanup_session_secrets", MagicMock()) as mock_clean:
            await oc.delete_sandbox(sandbox_name="sandbox-s42-abc1")
            mock_clean.assert_not_called()
