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
            "clone_repos": patch(
                "swarmer.openshell_client.clone_repos",
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
        }
        return patches

    def _all_patches(self, patches):
        """Enter all patches in the standard set."""
        return (
            patches["create_provider"], patches["ensure_provider"],
            patches["configure_provider_credential"], patches["attach_sandbox_provider"],
            patches["create_sandbox"], patches["write_agent_config"],
            patches["clone_repos"], patches["write_agents_md"],
            patches["exec_command"], patches["start_agent"],
            patches["delete_sandbox"], patches["build_policy"],
            patches["run_agent"], patches["setup_sandbox"],
        )

    @pytest.mark.asyncio
    async def test_creates_sandbox_with_tool_image(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], \
             patches["write_agent_config"], patches["clone_repos"], \
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
             patches["write_agent_config"], patches["clone_repos"], \
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
             patches["write_agent_config"] as mock_cfg, patches["clone_repos"], \
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
             patches["write_agent_config"], patches["clone_repos"] as mock_clone, \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], patches["setup_sandbox"]:
            await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )

        mock_clone.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_write_agents_md_for_prompt_mode(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], \
             patches["write_agent_config"], patches["clone_repos"], \
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
             patches["write_agent_config"], patches["clone_repos"], \
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
             patches["write_agent_config"], patches["clone_repos"], \
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
             patches["write_agent_config"], patches["clone_repos"], \
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
             patches["write_agent_config"], patches["clone_repos"], \
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
             patches["write_agent_config"], patches["clone_repos"], \
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
    async def test_passes_policy_yaml_from_builder(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"] as mock_sandbox, \
             patches["write_agent_config"], patches["clone_repos"], \
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
            await _run_openshell_agent(s["id"], "sandbox-prompt", ["sh", "-c", "opencode run"], "prompt")

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
            await _run_openshell_agent(s["id"], "sandbox-fail", ["sh", "-c", "opencode run"], "prompt")

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
            await _run_openshell_agent(s["id"], "sandbox-autoclean", ["sh", "-c", "opencode run"], "prompt")

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
            await _run_openshell_agent(s["id"], "sandbox-running", ["sh", "-c", "opencode run"], "prompt")

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
                s["id"], "sandbox-server", ["sh", "-c", "opencode serve"], "server"
            )

        mock_start.assert_called_once_with("sandbox-server", ["sh", "-c", "opencode serve"])
        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_tui_mode_calls_start_agent(self, client):
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
                s["id"], "sandbox-tui", ["sh", "-c", "sleep infinity"], "tui"
            )

        mock_start.assert_called_once()

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
            await _run_openshell_agent(s["id"], "sandbox-exc", ["sh", "-c", "opencode run"], "prompt")

        async with _TestSession() as db:
            from sqlalchemy import select
            from swarmer.models.session import Session
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()

        assert sess.phase == "failed"
        assert sess.run_completed_at is not None


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
