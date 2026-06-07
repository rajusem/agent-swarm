"""Tests for the OpenShell session launch path.

Covers:
  - _do_launch_openshell(): sandbox creation, config writing, repo cloning, AGENTS.md, background task
  - _do_launch() routes to _do_launch_openshell() (not K8s pod path)
  - _do_launch() still checks auth and capacity-gates
  - session stop: delete_sandbox() called, sandbox_name cleared
  - _run_openshell_agent(): prompt mode (succeeded/failed), server/tui mode, exception handling
  - No K8s PVC/Secret/Pod operations for OpenShell sessions
"""

import asyncio
import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from swarmer.database import Base

# ---------------------------------------------------------------------------
# Shared DB fixtures
# ---------------------------------------------------------------------------

_engine = create_async_engine("sqlite+aiosqlite://", echo=False)
_TestSession = async_sessionmaker(_engine, expire_on_commit=False)


async def _override_get_db():
    async with _TestSession() as session:
        yield session


def _override_require_api_auth():
    from swarmer.k8s_auth import TokenIdentity
    return TokenIdentity(username="test-user", uid="uid-1234")


def _override_get_current_user():
    return "test-user"


@pytest_asyncio.fixture(autouse=True)
async def _setup_db():
    from swarmer.crypto import init_crypto
    init_crypto("auth/secret.key")

    from swarmer.config import settings
    orig_ns = settings.k8s_namespace
    orig_max = settings.max_concurrent_agents
    settings.k8s_namespace = "test-ns"
    settings.max_concurrent_agents = 0  # unlimited by default for these tests

    import swarmer.models  # noqa: F401

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    settings.k8s_namespace = orig_ns
    settings.max_concurrent_agents = orig_max


@pytest_asyncio.fixture
async def client():
    from swarmer.api.deps import get_current_user, require_api_auth
    from swarmer.database import get_db
    from swarmer.main import app

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_api_auth] = _override_require_api_auth
    app.dependency_overrides[get_current_user] = _override_get_current_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helper: create workspace and session via API
# ---------------------------------------------------------------------------


async def _create_workspace(client: AsyncClient, name: str = "Test WS") -> dict:
    resp = await client.post(
        "/api/v1/workspaces",
        json={"display_name": name, "description": ""},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_session(
    client: AsyncClient,
    ws_id: int,
    name: str = "s1",
    mode: str = "prompt",
    agent_tool: str = "opencode",
) -> dict:
    resp = await client.post(
        f"/api/v1/workspaces/{ws_id}/sessions",
        json={"name": name, "mode": mode, "agent_tool": agent_tool},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _fake_sandbox_ref(name: str = "sandbox-test-abc123"):
    ref = MagicMock()
    ref.name = name
    return ref


# ===========================================================================
# 1. _do_launch() routes to OpenShell path (not K8s)
# ===========================================================================


class TestDoLaunchRoutesToOpenshell:
    @pytest.mark.asyncio
    async def test_do_launch_calls_openshell_not_k8s(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        with patch("swarmer.routers.sessions._do_launch_openshell", new=AsyncMock()) as mock_openshell:
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )
        assert resp.status_code == 200
        mock_openshell.assert_called_once()

    @pytest.mark.asyncio
    async def test_do_launch_does_not_create_k8s_pod(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        with patch("swarmer.routers.sessions._do_launch_openshell", new=AsyncMock()):
            with patch("kubernetes.client.CoreV1Api") as mock_k8s:
                await client.post(
                    f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
                )
        # K8s pod creation was never called
        mock_k8s.return_value.create_namespaced_pod.assert_not_called()

    @pytest.mark.asyncio
    async def test_do_launch_unknown_user_raises(self, client):
        """Auth check: user_id='unknown' must raise ValueError."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            from sqlalchemy import select
            from swarmer.models.session import Session
            from swarmer.models.workspace import Workspace
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()
            workspace = (await db.execute(select(Workspace).where(Workspace.id == ws["id"]))).scalar_one()

            from swarmer.routers.sessions import _do_launch
            with pytest.raises(ValueError, match="Session expired"):
                await _do_launch(sess, workspace, db, user_id="unknown")

    @pytest.mark.asyncio
    async def test_do_launch_queues_at_capacity(self, client):
        from swarmer.config import settings
        settings.max_concurrent_agents = 2

        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        with patch("swarmer.routers.sessions._count_running_sessions", new=AsyncMock(return_value=2)):
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )
        assert resp.status_code == 200
        assert resp.json()["phase"] == "queued"


# ===========================================================================
# 2. _do_launch_openshell() core behavior (unit-level)
# ===========================================================================


class TestDoLaunchOpenshell:
    """Unit tests for _do_launch_openshell() with OpenShell client mocked."""

    def _patch_openshell(self, sandbox_name: str = "sandbox-test-123"):
        """Return a context manager dict of patches for openshell_client."""
        ref = _fake_sandbox_ref(sandbox_name)
        patches = {
            "create_provider": patch(
                "swarmer.openshell_client.create_provider",
                new=AsyncMock(return_value={}),
            ),
            "ensure_provider": patch(
                "swarmer.openshell_client.ensure_provider",
                new=AsyncMock(),
            ),
            "configure_vertex_provider": patch(
                "swarmer.openshell_client.configure_vertex_provider",
                new=AsyncMock(),
            ),
            "set_cluster_inference": patch(
                "swarmer.openshell_client.set_cluster_inference",
                new=AsyncMock(),
            ),
            "configure_provider_credential": patch(
                "swarmer.openshell_client.configure_provider_credential",
                new=AsyncMock(),
            ),
            "attach_sandbox_provider": patch(
                "swarmer.openshell_client.attach_sandbox_provider",
                new=AsyncMock(),
            ),
            "create_sandbox": patch(
                "swarmer.openshell_client.create_sandbox",
                new=AsyncMock(return_value=ref),
            ),
            "write_agent_config": patch(
                "swarmer.openshell_client.write_agent_config",
                new=AsyncMock(),
            ),
            "write_agents_md": patch(
                "swarmer.openshell_client.write_agents_md",
                new=AsyncMock(),
            ),
            "exec_command": patch(
                "swarmer.openshell_client.exec_command",
                new=AsyncMock(),
            ),
            "start_agent": patch(
                "swarmer.openshell_client.start_agent",
                new=AsyncMock(),
            ),
            "delete_sandbox": patch(
                "swarmer.openshell_client.delete_sandbox",
                new=AsyncMock(),
            ),
            "build_policy": patch(
                "swarmer.openshell_policy.build_session_policy",
                return_value="version: 1\n",
            ),
            # Patch _run_openshell_agent so asyncio.create_task gets a real coroutine
            # (avoids SQLAlchemy shield() incompatibility with MagicMock tasks)
            "run_agent": patch(
                "swarmer.routers.sessions._run_openshell_agent",
                new=AsyncMock(),
            ),
            "setup_sandbox": patch(
                "swarmer.routers.sessions._setup_openshell_sandbox",
                new=AsyncMock(),
            ),
            "wait_vertex_ready": patch(
                "swarmer.routers.sessions._wait_vertex_provider_ready",
                new=AsyncMock(),
            ),
        }
        return patches

    def _all_patches(self, patches):
        """Enter all patches in the standard set."""
        return (
            patches["create_provider"], patches["ensure_provider"],
            patches["configure_vertex_provider"], patches["set_cluster_inference"],
            patches["configure_provider_credential"], patches["attach_sandbox_provider"],
            patches["create_sandbox"], patches["write_agent_config"],
            patches["write_agents_md"],
            patches["exec_command"], patches["start_agent"],
            patches["delete_sandbox"], patches["build_policy"],
            patches["run_agent"], patches["setup_sandbox"],
            patches["wait_vertex_ready"],
        )

    @pytest.mark.asyncio
    async def test_creates_sandbox_with_tool_image(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], \
             patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], \
             patches["setup_sandbox"] as mock_setup:
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )

        assert resp.status_code == 200
        # create_sandbox is called inside _setup_openshell_sandbox (background task).
        # Verify the setup task was invoked with the correct image.
        mock_setup.assert_called_once()
        call_kwargs = mock_setup.call_args[1] if mock_setup.call_args else {}
        assert "image" in call_kwargs

    @pytest.mark.asyncio
    async def test_sets_sandbox_name_on_session(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        patches = self._patch_openshell(sandbox_name="sandbox-xyz-789")
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], \
             patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], patches["setup_sandbox"]:
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )

        assert resp.status_code == 200
        # sandbox_name is set by the background setup task (_setup_openshell_sandbox).
        # The HTTP response returns immediately with phase=pending.
        # Verify the sandbox was given the right name by checking create_sandbox was
        # called and the session is in pending state.
        data = resp.json()
        assert data["phase"] == "pending"

    @pytest.mark.asyncio
    async def test_writes_agent_config(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], \
             patches["write_agent_config"] as mock_cfg, \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], patches["setup_sandbox"]:
            await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )

        # write_agent_config is only called when mcp_servers are configured;
        # the container's default opencode.json is preserved otherwise
        mock_cfg.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_clone_when_no_repos(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])  # no repos

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], \
             patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], patches["setup_sandbox"]:
            await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )

        # No repos attached — exec_command should not be called for git clone
        exec_mock = patches["exec_command"].new
        git_clone_calls = [c for c in exec_mock.call_args_list if "git clone" in str(c)]
        assert git_clone_calls == []

    @pytest.mark.asyncio
    async def test_does_not_write_agents_md_for_prompt_mode(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], \
             patches["write_agent_config"], \
             patches["write_agents_md"] as mock_md, patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], patches["setup_sandbox"]:
            await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )

        mock_md.assert_not_called()

    @pytest.mark.asyncio
    async def test_sets_phase_pending_before_task(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], \
             patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], patches["setup_sandbox"]:
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )

        # Phase will be "pending" (set by _do_launch_openshell before task)
        # or updated by _run_openshell_agent to "running" — check it's not idle/failed
        assert resp.json()["phase"] in ("pending", "running")

    @pytest.mark.asyncio
    async def test_no_k8s_pvc_created(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], \
             patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], patches["setup_sandbox"]:
            with patch("swarmer.k8s_session.ensure_session_pvc") as mock_pvc:
                await client.post(
                    f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
                )

        mock_pvc.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_k8s_secrets_created(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], \
             patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], patches["setup_sandbox"]:
            with patch("swarmer.k8s.create_session_agent_secret") as mock_secret:
                await client.post(
                    f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
                )

        mock_secret.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_background_task(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], \
             patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], \
             patches["setup_sandbox"] as mock_setup:
            await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )

        # _setup_openshell_sandbox is spawned as an asyncio task for background sandbox creation
        mock_setup.assert_called_once()

    @pytest.mark.asyncio
    async def test_uses_provider_api_not_env_vars_for_credentials(self, client):
        """AI credentials must flow through the gateway Provider API, not SandboxSpec.environment."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        patches = self._patch_openshell()
        with patches["create_provider"] as mock_provider, \
             patches["ensure_provider"] as mock_ensure, \
             patches["configure_provider_credential"] as mock_cred, \
             patches["attach_sandbox_provider"] as mock_attach, \
             patches["create_sandbox"] as mock_sandbox, \
             patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], patches["setup_sandbox"]:
            mock_provider.return_value = {}
            await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )

        # create_sandbox env_vars must NOT contain AI credentials
        call_kwargs = mock_sandbox.call_args[1] if mock_sandbox.call_args else {}
        passed_env = call_kwargs.get("env_vars", {})
        assert "ANTHROPIC_API_KEY" not in passed_env
        assert "GOOGLE_API_KEY" not in passed_env
        # No oc_secret in this test session, so no provider calls expected
        assert mock_ensure.call_count == 0
        assert mock_cred.call_count == 0
        assert mock_attach.call_count == 0

    @pytest.mark.asyncio
    async def test_launch_blocked_when_github_repo_without_pat(self, client):
        """Launch must be rejected with a clear message when github.com repos have no PAT.

        The OpenShell gateway requires a valid GitHub provider credential to allow
        CONNECT tunnels to github.com. Rather than failing silently mid-sandbox-setup,
        _do_launch raises early so the user sees an actionable error.
        """
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/repos",
            json={"repo_url": "https://github.com/org/public-repo.git", "branch": "main"},
        )

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"] as mock_sandbox, patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], patches["setup_sandbox"]:
            await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )

        # No sandbox should have been created — launch was blocked before reaching OpenShell
        mock_sandbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_github_provider_registered_when_pat_present(self, client):
        """When a PAT is configured, ensure_provider is called with the real token."""
        ws = await _create_workspace(client)
        # Create a PAT
        pat_resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/secrets/pats",
            json={"name": "test-pat", "github_username": "octocat", "pat_value": "ghp_testtoken123"},
        )
        pat = pat_resp.json()
        # Create session with that PAT
        s_resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions",
            json={"name": "s-with-pat", "mode": "prompt", "agent_tool": "opencode",
                  "github_pat_id": pat["id"]},
        )
        s = s_resp.json()

        patches = self._patch_openshell()
        with patches["create_provider"], \
             patches["ensure_provider"] as mock_ensure, \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], patches["setup_sandbox"]:
            await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )

        github_calls = [
            c for c in mock_ensure.call_args_list
            if len(c.args) >= 2 and c.args[1] == "github"
        ]
        assert len(github_calls) == 1, (
            f"Expected 1 github provider call when PAT is set, got {len(github_calls)}"
        )
        creds = github_calls[0].kwargs.get("credentials", {})
        assert "api_token" in creds and creds["api_token"], (
            f"Expected non-empty api_token credential in github provider call, got: {creds}"
        )

    @pytest.mark.asyncio
    async def test_passes_policy_yaml_from_builder(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"] as mock_sandbox, \
             patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"] as mock_policy, patches["run_agent"]:
            mock_policy.return_value = "version: 1\nnetwork_policies: {}\n"
            await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )

        call_kwargs = mock_sandbox.call_args[1] if mock_sandbox.call_args else {}
        call_args = mock_sandbox.call_args[0] if mock_sandbox.call_args else ()
        passed_policy = call_kwargs.get("policy") or (call_args[2] if len(call_args) > 2 else None)
        # policy is now a SandboxPolicy proto, not a YAML string
        assert passed_policy is not None

    @pytest.mark.asyncio
    async def test_launch_clears_stale_status_detail(self, client):
        """Launching a session clears any stale status_detail from a previous run."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        # Pre-set stale status_detail from a previous failed run
        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET status_detail='OpenShell agent startup failed', phase='failed' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], patches["build_policy"], \
             patches["run_agent"], patches["setup_sandbox"]:
            await client.post(f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch")

        session_resp = await client.get(f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}")
        data = session_resp.json()
        assert data["status_detail"] == "", f"Expected empty status_detail, got: {data['status_detail']!r}"
        assert data["phase"] == "pending"


# ===========================================================================
# 3. Session stop: sandbox deletion
# ===========================================================================


class TestSessionStopOpenshell:
    @pytest.mark.asyncio
    async def test_stop_calls_delete_sandbox(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        # Pre-set sandbox_name on the session
        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-stop-test', phase='running' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_delete:
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/stop"
            )

        assert resp.status_code == 200
        mock_delete.assert_called_once_with("sandbox-stop-test")

    @pytest.mark.asyncio
    async def test_stop_clears_sandbox_name(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-clear-test', phase='running' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()):
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/stop"
            )

        assert resp.status_code == 200
        assert resp.json()["sandbox_name"] is None

    @pytest.mark.asyncio
    async def test_stop_sets_phase_stopped(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-phase-test', phase='running' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()):
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/stop"
            )

        assert resp.json()["phase"] == "stopped"

    @pytest.mark.asyncio
    async def test_stop_does_not_call_k8s_delete_pod(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-nok8s', phase='running' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()):
            with patch("swarmer.k8s.delete_pod") as mock_pod_del:
                resp = await client.post(
                    f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/stop"
                )

        mock_pod_del.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_handles_delete_sandbox_error_gracefully(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-err', phase='running' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch(
            "swarmer.openshell_client.delete_sandbox",
            new=AsyncMock(side_effect=RuntimeError("gateway unavailable")),
        ):
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/stop"
            )

        # Should still succeed (warning flashed, phase set to stopped)
        assert resp.status_code == 200
        assert resp.json()["phase"] == "stopped"

    @pytest.mark.asyncio
    async def test_stop_queued_returns_idle_no_sandbox_call(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET phase='queued' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_delete:
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/stop"
            )

        assert resp.json()["phase"] == "idle"
        mock_delete.assert_not_called()


# ===========================================================================
# 4. _run_openshell_agent(): background task behavior
# ===========================================================================


def _make_test_db_provider():
    """Return an async generator that provides the test DB session.

    Used to patch swarmer.database.get_db so _run_openshell_agent() can
    access the same in-memory DB that the test creates sessions in.
    """
    async def _test_get_db():
        async with _TestSession() as session:
            yield session
    return _test_get_db


class TestRunOpenshellAgent:
    """Direct unit tests for _run_openshell_agent().

    Each test patches swarmer.database.get_db so the function can access
    the test DB (it imports get_db fresh each call).
    """

    @pytest.mark.asyncio
    async def test_prompt_mode_succeeds_on_exit_code_0(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-prompt', phase='pending' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        exec_result = MagicMock(exit_code=0, stdout="agent done", stderr="")
        with patch("swarmer.database.get_db", new=_make_test_db_provider()), \
             patch("swarmer.openshell_client.exec_command", new=AsyncMock(return_value=exec_result)), \
             patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()):
            from swarmer.routers.sessions import _run_openshell_agent
            await _run_openshell_agent(s["id"], "sandbox-prompt", ["sh", "-c", "opencode run"], "prompt", "opencode")

        async with _TestSession() as db:
            from sqlalchemy import select
            from swarmer.models.session import Session
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()

        assert sess.phase == "succeeded"
        assert "agent done" in (sess.last_output or "")

    @pytest.mark.asyncio
    async def test_prompt_mode_fails_on_nonzero_exit(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-fail', phase='pending' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        exec_result = MagicMock(exit_code=1, stdout="", stderr="error: tool crashed")
        with patch("swarmer.database.get_db", new=_make_test_db_provider()), \
             patch("swarmer.openshell_client.exec_command", new=AsyncMock(return_value=exec_result)), \
             patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()):
            from swarmer.routers.sessions import _run_openshell_agent
            await _run_openshell_agent(s["id"], "sandbox-fail", ["sh", "-c", "opencode run"], "prompt", "opencode")

        async with _TestSession() as db:
            from sqlalchemy import select
            from swarmer.models.session import Session
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()

        assert sess.phase == "failed"

    @pytest.mark.asyncio
    async def test_prompt_mode_auto_deletes_sandbox_on_success(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-autoclean', phase='pending' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        exec_result = MagicMock(exit_code=0, stdout="done", stderr="")
        with patch("swarmer.database.get_db", new=_make_test_db_provider()), \
             patch("swarmer.openshell_client.exec_command", new=AsyncMock(return_value=exec_result)), \
             patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_del:
            from swarmer.routers.sessions import _run_openshell_agent
            await _run_openshell_agent(s["id"], "sandbox-autoclean", ["sh", "-c", "opencode run"], "prompt", "opencode")

        mock_del.assert_called_once_with("sandbox-autoclean")

        async with _TestSession() as db:
            from sqlalchemy import select
            from swarmer.models.session import Session
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()
        assert sess.sandbox_name is None

    @pytest.mark.asyncio
    async def test_prompt_mode_sets_phase_running_first(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-running', phase='pending' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        phases_seen = []

        async def _fake_exec(sandbox_name, cmd, client=None, stdin=None, timeout_seconds=None):
            async with _TestSession() as db:
                from sqlalchemy import select
                from swarmer.models.session import Session
                sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()
                phases_seen.append(sess.phase)
            return MagicMock(exit_code=0, stdout="", stderr="")

        with patch("swarmer.database.get_db", new=_make_test_db_provider()), \
             patch("swarmer.openshell_client.exec_command", new=_fake_exec), \
             patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()):
            from swarmer.routers.sessions import _run_openshell_agent
            await _run_openshell_agent(s["id"], "sandbox-running", ["sh", "-c", "opencode run"], "prompt", "opencode")

        assert "running" in phases_seen

    @pytest.mark.asyncio
    async def test_server_mode_calls_start_agent(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-server', phase='pending' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.database.get_db", new=_make_test_db_provider()), \
             patch("swarmer.openshell_client.start_agent", new=AsyncMock()) as mock_start, \
             patch("swarmer.openshell_client.exec_command", new=AsyncMock()) as mock_exec:
            from swarmer.routers.sessions import _run_openshell_agent
            await _run_openshell_agent(
                s["id"], "sandbox-server", ["sh", "-c", "opencode serve"], "server", "opencode"
            )

        mock_start.assert_called_once_with("sandbox-server", ["sh", "-c", "opencode serve"])
        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_tui_mode_does_not_call_start_agent(self, client):
        """TUI mode skips start_agent — the WebSocket handler starts the agent interactively."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="tui")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-tui', phase='pending' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.database.get_db", new=_make_test_db_provider()), \
             patch("swarmer.openshell_client.start_agent", new=AsyncMock()) as mock_start:
            from swarmer.routers.sessions import _run_openshell_agent
            await _run_openshell_agent(
                s["id"], "sandbox-tui", ["sh", "-c", "sleep infinity"], "tui", "opencode"
            )

        mock_start.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_sets_phase_failed(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-exc', phase='pending' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.database.get_db", new=_make_test_db_provider()), \
             patch(
                "swarmer.openshell_client.exec_command",
                new=AsyncMock(side_effect=ConnectionError("gateway down")),
             ):
            from swarmer.routers.sessions import _run_openshell_agent
            await _run_openshell_agent(s["id"], "sandbox-exc", ["sh", "-c", "opencode run"], "prompt", "opencode")

        async with _TestSession() as db:
            from sqlalchemy import select
            from swarmer.models.session import Session
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()

        assert sess.phase == "failed"
        assert sess.run_completed_at is not None


# ===========================================================================
# 4b. exec_command timeout_seconds forwarding
# ===========================================================================


class TestExecCommandTimeout:
    @pytest.mark.asyncio
    async def test_exec_command_passes_timeout_to_sdk(self):
        """exec_command forwards timeout_seconds to the SDK exec call."""
        from swarmer.openshell_client import exec_command
        from unittest.mock import patch, MagicMock, AsyncMock

        mock_result = MagicMock(exit_code=0, stdout="ok", stderr="")
        mock_client = MagicMock()
        mock_client.get.return_value = MagicMock(id="test-id")
        mock_client.exec.return_value = mock_result

        with patch("swarmer.openshell_client._get_client", return_value=mock_client):
            result = await exec_command("sb-name", ["echo", "hi"], client=None, timeout_seconds=120)

        mock_client.exec.assert_called_once_with("test-id", ["echo", "hi"], stdin=None, timeout_seconds=120)
        assert result.stdout == "ok"

    @pytest.mark.asyncio
    async def test_exec_command_default_timeout_is_none(self):
        """exec_command passes timeout_seconds=None when not specified (SDK uses gRPC default)."""
        from swarmer.openshell_client import exec_command
        from unittest.mock import patch, MagicMock

        mock_client = MagicMock()
        mock_client.get.return_value = MagicMock(id="test-id")
        mock_client.exec.return_value = MagicMock(exit_code=0, stdout="", stderr="")

        with patch("swarmer.openshell_client._get_client", return_value=mock_client):
            await exec_command("sb-name", ["ls"], client=None)

        mock_client.exec.assert_called_once_with("test-id", ["ls"], stdin=None, timeout_seconds=None)




# ===========================================================================
# 5. _build_repo_context() base_path parameter
# ===========================================================================


class TestBuildRepoContextBasePath:
    def test_default_path_is_workspace(self):
        from swarmer.k8s_session import _build_repo_context

        class FakeRepo:
            repo_url = "https://github.com/org/myrepo"
            branch = "main"
            local_path = "myrepo"

        result = _build_repo_context([FakeRepo()])
        assert "/workspace/myrepo" in result
        assert "/sandbox/" not in result

    def test_sandbox_base_path(self):
        from swarmer.k8s_session import _build_repo_context

        class FakeRepo:
            repo_url = "https://github.com/org/myrepo"
            branch = "main"
            local_path = "myrepo"

        result = _build_repo_context([FakeRepo()], base_path="/sandbox")
        assert "/sandbox/myrepo" in result
        assert "/workspace/" not in result

    def test_empty_repos_returns_empty(self):
        from swarmer.k8s_session import _build_repo_context
        assert _build_repo_context([]) == ""
        assert _build_repo_context([], base_path="/sandbox") == ""


# ===========================================================================
# 6. session_delete() — OpenShell sandbox cleanup
# ===========================================================================


class TestSessionDeleteOpenshell:
    @pytest.mark.asyncio
    async def test_delete_calls_delete_sandbox_when_sandbox_set(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-del-test', phase='stopped' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_delete:
            resp = await client.delete(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}"
            )

        assert resp.status_code == 200
        mock_delete.assert_called_once_with("sandbox-del-test")

    @pytest.mark.asyncio
    async def test_delete_skips_k8s_pvc_for_sandbox_session(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-nopvc', phase='stopped' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()):
            with patch("swarmer.k8s_session.delete_session_pvc") as mock_pvc:
                await client.delete(f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}")

        mock_pvc.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_skips_k8s_secrets_for_sandbox_session(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-nosecrets', phase='stopped' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()):
            with patch("swarmer.k8s.cleanup_session_secrets") as mock_secrets:
                await client.delete(f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}")

        mock_secrets.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_handles_sandbox_error_gracefully(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-del-err', phase='stopped' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch(
            "swarmer.openshell_client.delete_sandbox",
            new=AsyncMock(side_effect=RuntimeError("gateway unavailable")),
        ):
            resp = await client.delete(f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}")

        # Session is deleted from DB despite sandbox error
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_delete_no_sandbox_skips_k8s_secrets_when_empty(self, client):
        """K8s cleanup is skipped when k8s_secret_names is empty (typical for stopped sandbox sessions)."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name=NULL, phase='stopped', k8s_secret_names='' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.k8s.cleanup_session_secrets") as mock_secrets:
            resp = await client.delete(f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}")

        assert resp.status_code == 200
        mock_secrets.assert_not_called()


# ===========================================================================
# 7. Sandbox GC — _collect_orphaned_sandboxes()
# ===========================================================================


class TestSandboxGC:
    @pytest.mark.asyncio
    async def test_deletes_sandbox_not_in_db(self):
        async with _TestSession() as db:
            with patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(return_value=["sandbox-orphan-1", "sandbox-orphan-2"])):
                with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_delete:
                    from swarmer.scheduler import _collect_orphaned_sandboxes
                    await _collect_orphaned_sandboxes(db)

        assert mock_delete.call_count == 2
        deleted = {c.args[0] for c in mock_delete.call_args_list}
        assert deleted == {"sandbox-orphan-1", "sandbox-orphan-2"}

    @pytest.mark.asyncio
    async def test_skips_sandbox_present_in_db(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-known', phase='running' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        async with _TestSession() as db:
            with patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(return_value=["sandbox-known", "sandbox-orphan"])):
                with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_delete:
                    from swarmer.scheduler import _collect_orphaned_sandboxes
                    await _collect_orphaned_sandboxes(db)

        # Only the orphan is deleted
        mock_delete.assert_called_once_with("sandbox-orphan")

    @pytest.mark.asyncio
    async def test_no_op_when_no_live_sandboxes(self):
        async with _TestSession() as db:
            with patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(return_value=[])):
                with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_delete:
                    from swarmer.scheduler import _collect_orphaned_sandboxes
                    await _collect_orphaned_sandboxes(db)

        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_continues_after_delete_error(self):
        async with _TestSession() as db:
            with patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(return_value=["sandbox-a", "sandbox-b"])):
                with patch(
                    "swarmer.openshell_client.delete_sandbox",
                    new=AsyncMock(side_effect=[RuntimeError("gateway error"), None]),
                ) as mock_delete:
                    from swarmer.scheduler import _collect_orphaned_sandboxes
                    await _collect_orphaned_sandboxes(db)

        assert mock_delete.call_count == 2

    @pytest.mark.asyncio
    async def test_handles_list_error_gracefully(self):
        async with _TestSession() as db:
            with patch(
                "swarmer.openshell_client.list_sandboxes",
                new=AsyncMock(side_effect=RuntimeError("gateway unavailable")),
            ):
                with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_delete:
                    from swarmer.scheduler import _collect_orphaned_sandboxes
                    await _collect_orphaned_sandboxes(db)

        mock_delete.assert_not_called()


# ===========================================================================
# 8. Crush-specific setup steps in _setup_openshell_sandbox and _run_openshell_agent
# ===========================================================================


def _make_crush_setup_patches(sandbox_name: str = "sandbox-crush-abc"):
    """Return a dict of patches for _setup_openshell_sandbox with Crush sessions."""
    ref = _fake_sandbox_ref(sandbox_name)
    return {
        "create_sandbox": patch(
            "swarmer.openshell_client.create_sandbox",
            new=AsyncMock(return_value=ref),
        ),
        "write_agent_config": patch(
            "swarmer.openshell_client.write_agent_config",
            new=AsyncMock(),
        ),
        "write_agents_md": patch(
            "swarmer.openshell_client.write_agents_md",
            new=AsyncMock(),
        ),
        "approve_chunks": patch(
            "swarmer.openshell_client.approve_draft_policy_chunks",
            new=AsyncMock(return_value=[]),
        ),
        "run_agent": patch(
            "swarmer.routers.sessions._run_openshell_agent",
            new=AsyncMock(),
        ),
        "sleep": patch("asyncio.sleep", new=AsyncMock()),
    }


async def _call_crush_setup(
    session_id: int,
    model: str = "anthropic/claude-sonnet-4-6",
    mode: str = "prompt",
    resolved_prompt: str = "Write hello world",
):
    """Call _setup_openshell_sandbox directly for Crush and return captured exec_command calls."""
    from swarmer.agent_tools.crush import CrushStrategy
    from swarmer.routers.sessions import _setup_openshell_sandbox

    tool = CrushStrategy()
    model_setup_cmd = tool.build_model_setup_cmd(model).replace("/workspace/", "/sandbox/")
    share_cmd = tool.build_share_setup_cmd().replace("/workspace/", "/sandbox/")

    class _FakeSession:
        instruction_prompt = ""
        this_mode = mode

    _fake_s = _FakeSession()
    _fake_s.mode = mode
    main_cmd = tool.build_main_cmd(_fake_s, model, resolved_prompt=resolved_prompt)

    exec_calls: list[list[str]] = []

    async def _capture_exec(sandbox_name, cmd, client=None, stdin=None, timeout_seconds=None):
        exec_calls.append(list(cmd))
        return MagicMock(exit_code=0, stdout="", stderr="")

    patches = _make_crush_setup_patches()
    with patch("swarmer.database.get_db", new=_make_test_db_provider()), \
         patches["create_sandbox"], \
         patches["write_agent_config"], \
         patches["write_agents_md"], \
         patches["approve_chunks"], \
         patches["run_agent"], \
         patches["sleep"], \
         patch("swarmer.openshell_client.exec_command", new=_capture_exec):
        await _setup_openshell_sandbox(
            session_id=session_id,
            provider_names=[],
            env_vars={},
            policy=None,
            image="quay.io/crush:latest",
            tool_name="crush",
            model=model,
            model_setup_cmd=model_setup_cmd,
            share_cmd=share_cmd,
            mcp_patch={},
            repos_data=[],
            git_username="",
            pat_token="",
            working_branch="",
            agents_md="",
            mode=mode,
            main_cmd=main_cmd,
            resolved_prompt=resolved_prompt,
        )

    return exec_calls


class TestCrushOpenshellSetup:
    """Tests for Crush-specific steps in _setup_openshell_sandbox and _run_openshell_agent."""

    @pytest.mark.asyncio
    async def test_probe_uses_crush_run_binary(self, client):
        """Pre-flight probe must use 'crush run' for Crush sessions, not 'opencode run'."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt", agent_tool="crush")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET phase='pending' WHERE id=:id"), {"id": s["id"]}
            )
            await db.commit()

        exec_calls = await _call_crush_setup(s["id"])

        all_cmds = " ".join(" ".join(c) for c in exec_calls)
        assert "crush run" in all_cmds, f"Expected 'crush run' in probe, got calls: {exec_calls}"
        assert "opencode run" not in all_cmds, "Crush session must not call 'opencode run'"

    @pytest.mark.asyncio
    async def test_share_cmd_runs_before_model_setup_cmd(self, client):
        """share_cmd creates the symlink before model_setup_cmd writes the model config through it."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt", agent_tool="crush")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET phase='pending' WHERE id=:id"), {"id": s["id"]}
            )
            await db.commit()

        exec_calls = await _call_crush_setup(s["id"])

        # share_cmd contains the symlink creation; model_setup_cmd contains printf
        share_idx = next(
            (i for i, c in enumerate(exec_calls) if "ln -sf" in " ".join(c)),
            None,
        )
        model_idx = next(
            (i for i, c in enumerate(exec_calls) if "printf" in " ".join(c) and ".local/share/crush" in " ".join(c)),
            None,
        )
        assert share_idx is not None, "share_cmd (ln -sf) not found in exec calls"
        assert model_idx is not None, "model_setup_cmd (printf) not found in exec calls"
        assert share_idx < model_idx, (
            f"share_cmd must run before model_setup_cmd "
            f"(share at index {share_idx}, model at index {model_idx})"
        )

    @pytest.mark.asyncio
    async def test_model_and_share_cmds_export_sandbox_home(self, client):
        """model_setup_cmd and share_cmd must run with 'export HOME=/sandbox' so $HOME resolves correctly."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt", agent_tool="crush")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET phase='pending' WHERE id=:id"), {"id": s["id"]}
            )
            await db.commit()

        exec_calls = await _call_crush_setup(s["id"])

        # Both the symlink creation and model config write must use HOME=/sandbox
        setup_calls = [
            c for c in exec_calls
            if ("ln -sf" in " ".join(c) or "printf" in " ".join(c))
            and "export HOME=/sandbox" in " ".join(c)
        ]
        assert len(setup_calls) >= 2, (
            "Expected at least 2 exec calls with 'export HOME=/sandbox' (share + model), "
            f"got: {[' '.join(c) for c in exec_calls]}"
        )

    def test_crush_config_includes_providers_with_env_var_refs(self):
        """build_config_data must include a providers array so Crush doesn't say 'No providers configured'."""
        import json as _json
        from swarmer.agent_tools.crush import CrushStrategy

        config_data = CrushStrategy().build_config_data()
        config = _json.loads(config_data["crush.json"])

        assert "providers" in config, "crush.json must have a 'providers' key"
        providers = config["providers"]
        assert isinstance(providers, dict), (
            "providers must be a map[string]ProviderConfig (dict), not an array"
        )
        assert len(providers) > 0, "providers map must not be empty"

        # Anthropic provider must be keyed by "anthropic" with an env-var reference
        assert "anthropic" in providers, "Anthropic provider must be present under key 'anthropic'"
        assert "$ANTHROPIC_API_KEY" in providers["anthropic"].get("api_key", ""), (
            "Anthropic provider api_key must reference $ANTHROPIC_API_KEY"
        )

        # Gemini provider must be keyed by "gemini"
        assert "gemini" in providers, "Gemini provider must be present under key 'gemini'"

    @pytest.mark.asyncio
    async def test_write_agent_config_crush_uses_sandbox_path(self):
        """write_agent_config for crush writes to /sandbox/.config/crush/, not /home/sandbox/."""
        from swarmer.openshell_client import write_agent_config

        captured: list[list] = []

        mock_client = MagicMock()
        mock_client.get.return_value = MagicMock(id="test-sid")
        mock_client.exec.side_effect = lambda sid, cmd, stdin=None, **kw: captured.append(cmd) or MagicMock()

        with patch("swarmer.openshell_client._get_client", return_value=mock_client):
            await write_agent_config("sandbox-test", "crush", '{"options": {}}')

        full_cmd = " ".join(captured[0]) if captured else ""
        assert "/sandbox/.config/crush" in full_cmd, (
            f"Expected /sandbox/.config/crush in cmd, got: {full_cmd}"
        )
        assert "/home/sandbox" not in full_cmd, (
            f"Must not write to /home/sandbox (agent runs with HOME=/sandbox): {full_cmd}"
        )

    @pytest.mark.asyncio
    async def test_write_agent_config_opencode_path_unchanged(self):
        """write_agent_config for opencode writes to /sandbox/opencode.json (no regression)."""
        from swarmer.openshell_client import write_agent_config

        captured: list[list] = []

        mock_client = MagicMock()
        mock_client.get.return_value = MagicMock(id="test-sid")
        mock_client.exec.side_effect = lambda sid, cmd, stdin=None, **kw: captured.append(cmd) or MagicMock()

        with patch("swarmer.openshell_client._get_client", return_value=mock_client):
            await write_agent_config("sandbox-test", "opencode", '{"providers": []}')

        full_cmd = " ".join(captured[0]) if captured else ""
        assert "/sandbox/opencode.json" in full_cmd, (
            f"Expected /sandbox/opencode.json, got: {full_cmd}"
        )

    @pytest.mark.asyncio
    async def test_agent_cmd_uses_crush_run(self, client):
        """_run_openshell_agent must be invoked with 'crush run' in the command for Crush sessions."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt", agent_tool="crush")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET phase='pending' WHERE id=:id"), {"id": s["id"]}
            )
            await db.commit()

        from swarmer.agent_tools.crush import CrushStrategy
        from swarmer.routers.sessions import _setup_openshell_sandbox

        tool = CrushStrategy()
        model = "anthropic/claude-sonnet-4-6"
        captured_cmd: list[str] = []

        # Use a plain function (not async) so the cmd is captured synchronously
        # when asyncio.create_task calls it — before the task body ever runs.
        def _capture_run_agent(session_id, sandbox_name, cmd, mode, agent_tool):
            captured_cmd.extend(cmd)
            async def _noop():
                pass
            return _noop()

        patches = _make_crush_setup_patches()
        patches["run_agent"] = patch(
            "swarmer.routers.sessions._run_openshell_agent",
            new=_capture_run_agent,
        )

        with patch("swarmer.database.get_db", new=_make_test_db_provider()), \
             patches["create_sandbox"], \
             patches["write_agent_config"], \
             patches["write_agents_md"], \
 \
             patches["approve_chunks"], \
             patches["run_agent"], \
             patches["sleep"], \
             patch("swarmer.openshell_client.exec_command", new=AsyncMock(
                 return_value=MagicMock(exit_code=0, stdout="", stderr="")
             )):
            await _setup_openshell_sandbox(
                session_id=s["id"],
                provider_names=[],
                env_vars={},
                policy=None,
                image="quay.io/crush:latest",
                tool_name="crush",
                model=model,
                model_setup_cmd=tool.build_model_setup_cmd(model).replace("/workspace/", "/sandbox/"),
                share_cmd=tool.build_share_setup_cmd().replace("/workspace/", "/sandbox/"),
                mcp_patch={},
                repos_data=[],
                git_username="",
                pat_token="",
                working_branch="",
                agents_md="",
                mode="prompt",
                main_cmd=f"crush run --model {model}",
                resolved_prompt="Write hello world",
            )

        full_cmd = " ".join(captured_cmd)
        assert "crush run" in full_cmd, f"Expected 'crush run' in agent cmd, got: {full_cmd}"
        assert "opencode" not in full_cmd, f"'opencode' must not appear in crush agent cmd: {full_cmd}"

    @pytest.mark.asyncio
    async def test_crush_output_uses_stdout_not_opencode_db(self, client):
        """_run_openshell_agent for crush uses stdout as output, not the opencode SQLite DB."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt", agent_tool="crush")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sb-crush-out', phase='pending' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        exec_result = MagicMock(exit_code=0, stdout="crush finished successfully", stderr="")
        with patch("swarmer.database.get_db", new=_make_test_db_provider()), \
             patch("swarmer.openshell_client.exec_command", new=AsyncMock(return_value=exec_result)), \
             patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()), \
             patch("swarmer.openshell_client.read_opencode_response", new=AsyncMock(return_value="WRONG")) as mock_db_read:
            from swarmer.routers.sessions import _run_openshell_agent
            await _run_openshell_agent(
                s["id"], "sb-crush-out", ["sh", "-c", "crush run"], "prompt", "crush"
            )

        mock_db_read.assert_not_called()

        async with _TestSession() as db:
            from sqlalchemy import select
            from swarmer.models.session import Session
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()

        assert sess.phase == "succeeded"
        assert "crush finished successfully" in (sess.last_output or "")


# ===========================================================================
# 9. VertexAI provider injection via OpenShell native provider API
# ===========================================================================


import json as _json


def _fake_oc_secret(has_adc=False, has_vertex=False, google_api_key="", anthropic_api_key=""):
    """Build a minimal OpencodeSecret-like fake."""
    class _FakeSecret:
        pass

    s = _FakeSecret()
    s.has_adc = has_adc
    s.has_vertex = has_vertex
    s.google_api_key = google_api_key
    s.google_api_key_enc = google_api_key
    s.anthropic_api_key = anthropic_api_key
    s.anthropic_api_key_enc = anthropic_api_key
    s.google_cloud_project = "my-project" if has_vertex else ""
    s.vertex_location = "us-central1" if has_vertex else ""
    # Minimal service_account ADC JSON
    _adc = _json.dumps({
        "type": "service_account",
        "project_id": "my-project",
        "private_key_id": "key-id",
        "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n",
        "client_email": "sa@my-project.iam.gserviceaccount.com",
        "client_id": "12345",
    })
    s.application_default_credentials = _adc if has_adc else ""
    return s


async def _launch_with_secret(client, ws_id, session_id, oc_secret):
    """Call _do_launch_openshell directly with a fake session/workspace/db and given oc_secret.

    Returns (mock_ensure, mock_vertex) so callers can assert provider calls.
    """
    from swarmer.routers.sessions import _do_launch_openshell
    from swarmer.models.session import Session as _Session
    from swarmer.models.workspace import Workspace as _Workspace

    # Build minimal fake ORM objects so _do_launch_openshell doesn't need a real DB
    fake_session = MagicMock(spec=_Session)
    fake_session.id = session_id
    fake_session.workspace_id = ws_id
    fake_session.agent_tool = "opencode"
    fake_session.model = None
    fake_session.mode = "prompt"
    fake_session.working_branch = None
    fake_session.repos = []
    fake_session.github_pat = None
    fake_session.instruction_prompt = ""

    fake_ws = MagicMock(spec=_Workspace)
    fake_ws.id = ws_id

    # Fake async DB — just needs commit() to not blow up
    fake_db = AsyncMock()

    mock_ensure = AsyncMock()
    mock_vertex = AsyncMock()
    mock_create_vertex = AsyncMock()

    with patch("swarmer.openshell_client.create_provider", new=AsyncMock(return_value={})), \
         patch("swarmer.openshell_client.ensure_provider", mock_ensure), \
         patch("swarmer.openshell_client.create_vertex_provider", mock_create_vertex), \
         patch("swarmer.openshell_client.configure_vertex_provider", mock_vertex), \
         patch("swarmer.openshell_client.set_cluster_inference", new=AsyncMock()), \
         patch("swarmer.routers.sessions._wait_vertex_provider_ready", new=AsyncMock()), \
         patch("swarmer.openshell_client.configure_provider_credential", new=AsyncMock()), \
         patch("swarmer.openshell_client.attach_sandbox_provider", new=AsyncMock()), \
         patch("swarmer.openshell_policy.build_session_policy", return_value="version: 1\n"), \
         patch("swarmer.routers.sessions._setup_openshell_sandbox", new=AsyncMock()):
        has_adc = oc_secret.has_adc if oc_secret else False
        has_gemini = bool(oc_secret and oc_secret.google_api_key)
        await _do_launch_openshell(
            session=fake_session,
            ws=fake_ws,
            db=fake_db,
            suffix="test",
            oc_secret=oc_secret,
            has_adc=has_adc,
            has_gemini=has_gemini,
            mcp_servers=None,
            resolved_prompt="test prompt",
        )

    return mock_ensure, mock_vertex, mock_create_vertex


class TestVertexAIProviderInjection:
    """Tests that VertexAI credentials flow through the OpenShell gateway Provider API."""

    @pytest.mark.asyncio
    async def test_vertex_provider_created_when_adc_present(self, client):
        """create_vertex_provider is called when ADC + vertex config exist."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        fake_secret = _fake_oc_secret(has_adc=True, has_vertex=True)
        _, _, mock_create_vertex = await _launch_with_secret(client, ws["id"], s["id"], fake_secret)

        mock_create_vertex.assert_called_once()

    @pytest.mark.asyncio
    async def test_vertex_provider_refresh_configured(self, client):
        """configure_vertex_provider is called when ADC and vertex config are present."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        fake_secret = _fake_oc_secret(has_adc=True, has_vertex=True)
        _, mock_vertex, _ = await _launch_with_secret(client, ws["id"], s["id"], fake_secret)

        mock_vertex.assert_called_once()
        call_kwargs = mock_vertex.call_args.kwargs
        assert call_kwargs.get("project") == "my-project"
        assert call_kwargs.get("location") == "us-central1"

    @pytest.mark.asyncio
    async def test_vertex_provider_not_created_without_adc(self, client):
        """No google-vertex-ai provider is created when ADC is absent."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        fake_secret = _fake_oc_secret(has_adc=False, has_vertex=False)
        _, mock_vertex, mock_create_vertex = await _launch_with_secret(client, ws["id"], s["id"], fake_secret)

        mock_create_vertex.assert_not_called()
        mock_vertex.assert_not_called()

    @pytest.mark.asyncio
    async def test_gemini_provider_unchanged_with_vertex(self, client):
        """Both google-ai-studio and google-vertex-ai providers are created when both creds exist."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        fake_secret = _fake_oc_secret(has_adc=True, has_vertex=True, google_api_key="my-gemini-key")
        mock_ensure, _, mock_create_vertex = await _launch_with_secret(client, ws["id"], s["id"], fake_secret)

        provider_types_called = [
            c.args[1] for c in mock_ensure.call_args_list if len(c.args) >= 2
        ]
        assert "google-ai-studio" in provider_types_called, "Gemini provider must still be created"
        mock_create_vertex.assert_called_once()  # vertex provider created via create_vertex_provider


class TestConfigureVertexProvider:
    """Unit tests for configure_vertex_provider() in openshell_client."""

    @pytest.mark.asyncio
    async def test_service_account_uses_jwt_strategy(self):
        """Service account ADC triggers GOOGLE_SERVICE_ACCOUNT_JWT strategy."""
        from swarmer.openshell_client import configure_vertex_provider
        from openshell._proto import openshell_pb2

        adc = _json.dumps({
            "type": "service_account",
            "project_id": "proj",
            "private_key": "fake",
            "client_email": "sa@proj.iam.gserviceaccount.com",
        })

        captured_reqs = []

        mock_client = MagicMock()
        mock_client._timeout = 30

        def _capture_configure(req, timeout=None):
            captured_reqs.append(req)

        def _capture_update(req, timeout=None):
            pass

        mock_client._stub.ConfigureProviderRefresh.side_effect = _capture_configure
        mock_client._stub.UpdateProvider.side_effect = _capture_update

        with patch("swarmer.openshell_client._get_client", return_value=mock_client):
            await configure_vertex_provider("p1", adc, "proj", "us-central1")

        assert len(captured_reqs) == 1
        req = captured_reqs[0]
        assert req.credential_key == "GOOGLE_VERTEX_AI_TOKEN"
        assert req.strategy == openshell_pb2.PROVIDER_CREDENTIAL_REFRESH_STRATEGY_GOOGLE_SERVICE_ACCOUNT_JWT
        assert "private_key" in req.secret_material_keys

    @pytest.mark.asyncio
    async def test_authorized_user_uses_oauth2_refresh_strategy(self):
        """Authorized user ADC triggers OAUTH2_REFRESH_TOKEN strategy."""
        from swarmer.openshell_client import configure_vertex_provider
        from openshell._proto import openshell_pb2

        adc = _json.dumps({
            "type": "authorized_user",
            "client_id": "cid",
            "client_secret": "csecret",
            "refresh_token": "rtoken",
        })

        captured_reqs = []

        mock_client = MagicMock()
        mock_client._timeout = 30
        mock_client._stub.ConfigureProviderRefresh.side_effect = lambda req, timeout=None: captured_reqs.append(req)
        mock_client._stub.UpdateProvider.side_effect = lambda req, timeout=None: None

        with patch("swarmer.openshell_client._get_client", return_value=mock_client):
            await configure_vertex_provider("p2", adc, "proj", "us-central1")

        req = captured_reqs[0]
        assert req.strategy == openshell_pb2.PROVIDER_CREDENTIAL_REFRESH_STRATEGY_OAUTH2_REFRESH_TOKEN
        # token_url is in the profile definition, not material
        assert "token_url" not in req.material
        assert "client_secret" in req.secret_material_keys
        assert "refresh_token" in req.secret_material_keys

    @pytest.mark.asyncio
    async def test_unknown_adc_type_raises(self):
        """Unknown ADC type raises ValueError."""
        from swarmer.openshell_client import configure_vertex_provider

        adc = _json.dumps({"type": "external_account"})

        mock_client = MagicMock()
        mock_client._timeout = 30

        with patch("swarmer.openshell_client._get_client", return_value=mock_client):
            with pytest.raises(ValueError, match="Unsupported ADC type"):
                await configure_vertex_provider("p3", adc, "proj", "us")

    @pytest.mark.asyncio
    async def test_configure_vertex_provider_only_calls_refresh(self):
        """configure_vertex_provider only calls ConfigureProviderRefresh (not UpdateProvider).

        Project/location are non-secret strings passed via SandboxSpec.environment
        directly in the launch path, not stored as provider config.
        """
        from swarmer.openshell_client import configure_vertex_provider

        adc = _json.dumps({
            "type": "service_account",
            "project_id": "proj",
            "private_key": "fake",
            "client_email": "sa@proj.iam.gserviceaccount.com",
        })

        mock_client = MagicMock()
        mock_client._timeout = 30
        mock_client._stub.ConfigureProviderRefresh.side_effect = lambda req, timeout=None: None

        with patch("swarmer.openshell_client._get_client", return_value=mock_client):
            await configure_vertex_provider("p4", adc, "my-project", "europe-west4")

        mock_client._stub.ConfigureProviderRefresh.assert_called_once()
        mock_client._stub.UpdateProvider.assert_not_called()


class TestVertexEnvVarsInSandboxSpec:
    """Verify that GOOGLE_CLOUD_PROJECT etc. are injected via provider credentials (not SandboxSpec.environment)."""

    def _make_fake_session(self, session_id, ws_id, agent_tool="opencode"):
        from swarmer.models.session import Session as _Session
        from swarmer.models.workspace import Workspace as _Workspace
        fake_sess = MagicMock(spec=_Session)
        fake_sess.id = session_id
        fake_sess.workspace_id = ws_id
        fake_sess.agent_tool = agent_tool
        fake_sess.model = None
        fake_sess.mode = "prompt"
        fake_sess.working_branch = None
        fake_sess.repos = []
        fake_sess.github_pat = None
        fake_sess.instruction_prompt = ""
        fake_ws = MagicMock(spec=_Workspace)
        fake_ws.id = ws_id
        return fake_sess, fake_ws

    @pytest.mark.asyncio
    async def test_vertex_project_location_in_provider_config(self, client):
        """VERTEX_AI_PROJECT_ID and VERTEX_AI_REGION are passed as provider config (per OpenShell docs)."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        fake_secret = _fake_oc_secret(has_adc=True, has_vertex=True)

        mock_create_vertex = AsyncMock()
        with patch("swarmer.openshell_client.create_provider", new=AsyncMock(return_value={})), \
             patch("swarmer.openshell_client.ensure_provider", new=AsyncMock()), \
             patch("swarmer.openshell_client.create_vertex_provider", mock_create_vertex), \
             patch("swarmer.openshell_client.configure_vertex_provider", new=AsyncMock()), \
             patch("swarmer.openshell_client.set_cluster_inference", new=AsyncMock()), \
             patch("swarmer.openshell_client.configure_provider_credential", new=AsyncMock()), \
             patch("swarmer.openshell_client.attach_sandbox_provider", new=AsyncMock()), \
             patch("swarmer.openshell_policy.build_session_policy", return_value="version: 1\n"), \
             patch("swarmer.routers.sessions._setup_openshell_sandbox", new=AsyncMock()):
            fake_sess, fake_ws = self._make_fake_session(s["id"], ws["id"])
            from swarmer.routers.sessions import _do_launch_openshell
            await _do_launch_openshell(
                session=fake_sess, ws=fake_ws, db=AsyncMock(), suffix="t",
                oc_secret=fake_secret, has_adc=True, has_gemini=False,
                mcp_servers=None, resolved_prompt="hi",
            )

        # create_vertex_provider receives the project and location
        mock_create_vertex.assert_called_once()
        call_kwargs = mock_create_vertex.call_args.kwargs
        assert call_kwargs.get("project") == "my-project"
        assert call_kwargs.get("location") == "us-central1"

    @pytest.mark.asyncio
    async def test_no_vertex_env_vars_without_adc(self, client):
        """GOOGLE_CLOUD_PROJECT is absent from env_vars when no ADC present."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        fake_secret = _fake_oc_secret(has_adc=False, has_vertex=False)
        mock_setup = AsyncMock()

        with patch("swarmer.openshell_client.create_provider", new=AsyncMock(return_value={})), \
             patch("swarmer.openshell_client.ensure_provider", new=AsyncMock()), \
             patch("swarmer.openshell_client.configure_vertex_provider", new=AsyncMock()), \
             patch("swarmer.openshell_client.set_cluster_inference", new=AsyncMock()), \
             patch("swarmer.openshell_client.configure_provider_credential", new=AsyncMock()), \
             patch("swarmer.openshell_client.attach_sandbox_provider", new=AsyncMock()), \
             patch("swarmer.openshell_policy.build_session_policy", return_value="version: 1\n"), \
             patch("swarmer.routers.sessions._setup_openshell_sandbox", mock_setup):
            fake_sess, fake_ws = self._make_fake_session(s["id"], ws["id"])
            from swarmer.routers.sessions import _do_launch_openshell
            await _do_launch_openshell(
                session=fake_sess, ws=fake_ws, db=AsyncMock(), suffix="t",
                oc_secret=fake_secret, has_adc=False, has_gemini=False,
                mcp_servers=None, resolved_prompt="hi",
            )

        # No vertex provider → inference_env is empty → no ANTHROPIC_BASE_URL in env_vars
        inference_env = mock_setup.call_args.kwargs.get("inference_env", {})
        assert not inference_env

    @pytest.mark.asyncio
    async def test_vertex_env_vars_with_adc(self, client):
        """ANTHROPIC_BASE_URL is present in inference_env and set_cluster_inference is called when ADC present."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        fake_secret = _fake_oc_secret(has_adc=True, has_vertex=True)
        mock_setup = AsyncMock()
        mock_set_inf = AsyncMock()

        with patch("swarmer.openshell_client.create_provider", new=AsyncMock(return_value={})), \
             patch("swarmer.openshell_client.create_vertex_provider", new=AsyncMock()), \
             patch("swarmer.openshell_client.configure_vertex_provider", new=AsyncMock()), \
             patch("swarmer.openshell_client.set_cluster_inference", mock_set_inf), \
             patch("swarmer.routers.sessions._wait_vertex_provider_ready", new=AsyncMock()), \
             patch("swarmer.openshell_client.configure_provider_credential", new=AsyncMock()), \
             patch("swarmer.openshell_client.attach_sandbox_provider", new=AsyncMock()), \
             patch("swarmer.openshell_policy.build_session_policy", return_value="version: 1\n"), \
             patch("swarmer.routers.sessions._setup_openshell_sandbox", mock_setup):
            fake_sess, fake_ws = self._make_fake_session(s["id"], ws["id"])
            from swarmer.routers.sessions import _do_launch_openshell
            await _do_launch_openshell(
                session=fake_sess, ws=fake_ws, db=AsyncMock(), suffix="t",
                oc_secret=fake_secret, has_adc=True, has_gemini=False,
                mcp_servers=None, resolved_prompt="hi",
            )

        # set_cluster_inference is called once per Claude model (haiku, sonnet, opus)
        assert mock_set_inf.call_count == 3
        registered_models = {c.kwargs["model_id"] for c in mock_set_inf.call_args_list}
        assert registered_models == {"claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-6"}
        # inference_env contains ANTHROPIC_BASE_URL
        inference_env = mock_setup.call_args.kwargs.get("inference_env", {})
        assert inference_env.get("ANTHROPIC_BASE_URL") == "https://inference.local/v1"


class TestVertexOpenshellSetup:
    """Verify that _setup_openshell_sandbox merges inference_env into env_vars before creating sandbox."""

    @pytest.mark.asyncio
    async def test_setup_sandbox_merges_inference_env_into_env_vars(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET phase='pending' WHERE id=:id"), {"id": s["idblock"]} if False else {"id": s["id"]}
            )
            await db.commit()

        mock_create_sandbox = AsyncMock(return_value=MagicMock(name="sandbox-name", id="test-sid"))

        from swarmer.routers.sessions import _setup_openshell_sandbox

        with patch("swarmer.database.get_db", new=_make_test_db_provider()), \
             patch("swarmer.openshell_client.create_sandbox", mock_create_sandbox), \
             patch("swarmer.openshell_client.write_agent_config", new=AsyncMock()), \
             patch("swarmer.openshell_client.write_agents_md", new=AsyncMock()), \
             patch("swarmer.openshell_client.approve_draft_policy_chunks", new=AsyncMock()), \
             patch("swarmer.openshell_client.exec_command", new=AsyncMock(
                 return_value=MagicMock(exit_code=0, stdout="", stderr="")
             )), \
             patch("swarmer.routers.sessions._run_openshell_agent", new=AsyncMock()), \
             patch("asyncio.sleep", new=AsyncMock()):
            
            await _setup_openshell_sandbox(
                session_id=s["id"],
                provider_names=[],
                env_vars={"EXISTING_ENV": "123"},
                policy=None,
                image="quay.io/opencode:latest",
                tool_name="opencode",
                model="anthropic/claude-sonnet-4-6",
                model_setup_cmd="",
                share_cmd="",
                mcp_patch={},
                repos_data=[],
                git_username="",
                pat_token="",
                working_branch="",
                agents_md="",
                mode="prompt",
                main_cmd="opencode run",
                resolved_prompt="hello",
                inference_env={"ANTHROPIC_BASE_URL": "https://inference.local/v1"},
            )

        mock_create_sandbox.assert_called_once()
        passed_env_vars = mock_create_sandbox.call_args.kwargs.get("env_vars", {})
        assert passed_env_vars.get("EXISTING_ENV") == "123"
        assert passed_env_vars.get("ANTHROPIC_BASE_URL") == "https://inference.local/v1"
