"""Regression tests for OBSINTA-1337 / ACM-35375.

Covers:
  - migrate_db() drops legacy K8s columns (persist, privileged, pod_name,
    pvc_name, k8s_secret_names) that were removed from the Session model in
    ACM-34863 but whose DROP COLUMN migrations were missing.
  - migrate_db() is idempotent on a fresh schema (no error on missing columns).
  - POST /workspaces/{ws_id}/sessions with a duplicate name returns 422
    (not 500 from MissingGreenlet or Jinja2 UndefinedError).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# Stub openshell SDK before any swarmer imports so unit tests don't need
# the real gRPC package installed.
# ---------------------------------------------------------------------------
from unittest.mock import MagicMock as _MagicMock  # noqa: E402

_proto_stub = _MagicMock()
_proto_stub.openshell_pb2 = _MagicMock()


class _SandboxSpec:
    def __init__(self):
        class _T:
            image = ""

        self.template = _T()
        self.environment = {}
        self.policy = None
        self.providers = []


_proto_stub.openshell_pb2.SandboxSpec = _SandboxSpec

_sdk_stub = _MagicMock()
_sdk_stub.SandboxClient = _MagicMock
_sdk_stub.TlsConfig = _MagicMock
_sdk_stub._proto = _proto_stub

sys.modules["openshell"] = _sdk_stub
sys.modules["openshell._proto"] = _proto_stub
sys.modules["openshell._proto.openshell_pb2"] = _proto_stub.openshell_pb2

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from swarmer.database import Base, migrate_db

# ---------------------------------------------------------------------------
# Shared DB fixtures
# ---------------------------------------------------------------------------

_engine = create_async_engine("sqlite+aiosqlite://", echo=False)
_TestSession = async_sessionmaker(_engine, expire_on_commit=False)


async def _override_get_db():
    async with _TestSession() as session:
        yield session


def _override_require_auth():
    return None


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
    settings.k8s_namespace = "test-ns"

    import swarmer.models  # noqa: F401 — register models on Base.metadata

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    settings.k8s_namespace = orig_ns


@pytest_asyncio.fixture
async def client():
    from swarmer.api.deps import get_current_user, require_api_auth
    from swarmer.database import get_db
    from swarmer.deps import require_auth
    from swarmer.main import app

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_api_auth] = _override_require_api_auth
    app.dependency_overrides[get_current_user] = _override_get_current_user
    app.dependency_overrides[require_auth] = lambda: None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _create_workspace(client: AsyncClient, name: str = "Test WS") -> dict:
    resp = await client.post(
        "/api/v1/workspaces",
        json={"display_name": name, "description": ""},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ===========================================================================
# 1. migrate_db() drops legacy columns on pre-existing schema
# ===========================================================================


class TestMigrateDbDropsLegacyColumns:
    @pytest.mark.asyncio
    async def test_persist_column_dropped_by_migration(self):
        """migrate_db() removes the legacy 'persist' NOT NULL column.

        Simulates a database created before the ACM-34863 K8s cleanup by
        manually adding the legacy column with a NOT NULL constraint, then
        verifies that migrate_db() drops it so subsequent INSERTs succeed.
        """
        # Inject legacy column with NOT NULL (mirrors the pre-cleanup schema)
        async with _engine.begin() as conn:
            await conn.execute(
                text(
                    "ALTER TABLE sessions ADD COLUMN persist BOOLEAN NOT NULL DEFAULT 0"
                )
            )

        # Verify the column is present before migration
        async with _engine.connect() as conn:

            def _get_cols(sync_conn):
                insp = inspect(sync_conn)
                return [c["name"] for c in insp.get_columns("sessions")]

            cols_before = await conn.run_sync(_get_cols)
        assert "persist" in cols_before, "Setup: 'persist' column should exist before migration"

        # Run the migration — should drop 'persist' without error
        # We need to wire _engine into migrate_db via the module-level global
        import swarmer.database as _db_mod

        orig_engine = _db_mod._engine
        _db_mod._engine = _engine
        try:
            await migrate_db()
        finally:
            _db_mod._engine = orig_engine

        # Verify the column is gone after migration
        async with _engine.connect() as conn:
            cols_after = await conn.run_sync(_get_cols)
        assert "persist" not in cols_after, "'persist' column should be dropped after migration"

    @pytest.mark.asyncio
    async def test_migrate_db_idempotent_on_fresh_schema(self):
        """migrate_db() does not raise on a fresh schema without legacy columns.

        The error-suppression pattern ("no such column") must handle the case
        where DROP COLUMN targets a column that was never in the schema.
        """
        import swarmer.database as _db_mod

        orig_engine = _db_mod._engine
        _db_mod._engine = _engine
        try:
            # Should complete without raising any exception
            await migrate_db()
        finally:
            _db_mod._engine = orig_engine


# ===========================================================================
# 2. Duplicate session name via form POST returns 422, not 500
# ===========================================================================


class TestSessionFormCreatePath:
    @pytest.mark.asyncio
    async def test_form_create_duplicate_name_returns_422(self, client):
        """POST /workspaces/{ws_id}/sessions with a duplicate name must return
        422, not 500 from MissingGreenlet (expired ws ORM object) or Jinja2
        UndefinedError (missing mcp_servers/prompt_sources context).
        """
        from unittest.mock import AsyncMock, patch

        ws = await _create_workspace(client, name="FormTestWS")
        ws_id = ws["id"]

        # Create the first session via the API
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/sessions",
            json={"name": "duplicate-session", "mode": "prompt", "agent_tool": "opencode"},
        )
        assert resp.status_code == 201, resp.text

        from swarmer.config import settings as _settings

        orig_crush_image = _settings.agent_image_crush
        _settings.agent_image_crush = "quay.io/test/crush:test"
        try:
            # Patch k8s.get_image_available so the form handler doesn't need K8s
            with patch(
                "swarmer.k8s.get_image_available",
                new=AsyncMock(return_value=False),
            ):
                # Submit form POST with the same name — triggers the IntegrityError path
                form_resp = await client.post(
                    f"/workspaces/{ws_id}/sessions",
                    data={
                        "name": "duplicate-session",
                        "mode": "prompt",
                        "agent_tool": "opencode",
                        "model": "",
                        "instruction_prompt": "",
                        "github_pat_id": "",
                        "prompt_id": "",
                        "working_branch": "",
                    },
                )
        finally:
            _settings.agent_image_crush = orig_crush_image

        # Must return 422 (not 500 from MissingGreenlet or UndefinedError)
        assert form_resp.status_code == 422, (
            f"Expected 422 for duplicate session name, got {form_resp.status_code}. "
            f"Response: {form_resp.text[:500]}"
        )
