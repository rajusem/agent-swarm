import logging

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_engine = None
_AsyncSessionLocal: async_sessionmaker | None = None


def init_db(database_url: str) -> None:
    global _engine, _AsyncSessionLocal
    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args["timeout"] = 15
    _engine = create_async_engine(database_url, echo=False, connect_args=connect_args)

    if database_url.startswith("sqlite"):
        @event.listens_for(_engine.sync_engine, "connect")
        def _set_wal(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            # WAL lets readers and the scheduler writer proceed concurrently.
            cursor.execute("PRAGMA journal_mode=WAL")
            # Retry for up to 5 s before raising "database is locked".
            cursor.execute("PRAGMA busy_timeout=5000")
            # Clear any stale WAL state left by a previous unclean shutdown.
            cursor.execute("PRAGMA wal_checkpoint(PASSIVE)")
            cursor.close()

    _AsyncSessionLocal = async_sessionmaker(_engine, expire_on_commit=False)


async def create_tables() -> None:
    # Import models so their tables are registered on Base.metadata
    import swarmer.models  # noqa: F401

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def migrate_db() -> None:
    """Lightweight migrations for columns added after initial schema creation."""
    from sqlalchemy import text

    migrations = [
        "ALTER TABLE sessions ADD COLUMN privileged BOOLEAN NOT NULL DEFAULT 0",
        "ALTER TABLE sessions ADD COLUMN agent_tool VARCHAR(32) NOT NULL DEFAULT 'opencode'",
        "ALTER TABLE opencode_secrets ADD COLUMN anthropic_api_key_enc TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE opencode_secrets ADD COLUMN openai_api_key_enc TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN status_detail VARCHAR(255) NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN run_started_at DATETIME",
        "ALTER TABLE sessions ADD COLUMN run_completed_at DATETIME",
        "ALTER TABLE sessions ADD COLUMN working_branch VARCHAR(255) NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN patch_output TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN commit_msg TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN patch_base_ref VARCHAR(255) NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN cron_schedule VARCHAR(128) NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN cron_next_run DATETIME",
        "ALTER TABLE github_pats ADD COLUMN github_org TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN mcp_server_ids TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE mcp_servers ADD COLUMN jira_server_url TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE mcp_servers ADD COLUMN jira_access_token_enc TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE mcp_servers ADD COLUMN jira_email TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN k8s_secret_names TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE opencode_secrets ADD COLUMN user_id VARCHAR(255) NOT NULL DEFAULT ''",
        "ALTER TABLE opencode_secrets ADD COLUMN shared BOOLEAN NOT NULL DEFAULT 1",
        "ALTER TABLE github_pats ADD COLUMN user_id VARCHAR(255) NOT NULL DEFAULT ''",
        "ALTER TABLE github_pats ADD COLUMN shared BOOLEAN NOT NULL DEFAULT 1",
        "ALTER TABLE mcp_servers ADD COLUMN user_id VARCHAR(255) NOT NULL DEFAULT ''",
        "ALTER TABLE mcp_servers ADD COLUMN shared BOOLEAN NOT NULL DEFAULT 1",
        "ALTER TABLE sessions ADD COLUMN prompt_id INTEGER REFERENCES workspace_prompts(id) ON DELETE SET NULL",
        "ALTER TABLE sessions DROP COLUMN resume",
        "ALTER TABLE sessions ADD COLUMN sandbox_name VARCHAR(255)",
        "ALTER TABLE sessions ADD COLUMN service_url VARCHAR(512)",
        "ALTER TABLE sessions ADD COLUMN policy_chunks TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN custom_policies TEXT NOT NULL DEFAULT ''",
    ]
    async with _engine.begin() as conn:
        for stmt in migrations:
            try:
                await conn.execute(text(stmt))
            except Exception as e:
                msg = str(e).lower()
                if "duplicate column" in msg or "already exists" in msg or "no such column" in msg:
                    continue
                log.error("Migration failed for %r: %s", stmt, e)
                raise


async def checkpoint_db() -> None:
    """Force a WAL TRUNCATE checkpoint at startup.

    Consolidates any stale .db-wal data left by an unclean shutdown into the
    main database file, then truncates the WAL so subsequent connections start
    clean. Must be called after init_db() and before the server starts serving
    requests. No-op for non-SQLite engines.
    """
    if _engine is None:
        return
    url = str(_engine.url)
    if not url.startswith("sqlite"):
        return
    from sqlalchemy import text
    try:
        async with _engine.connect() as conn:
            result = await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
            row = result.fetchone()
            # row = (busy, log_pages, checkpointed_pages)
            if row and row[0]:
                log.warning("db: WAL checkpoint incomplete — %d page(s) still busy", row[0])
            else:
                log.info("db: WAL checkpoint complete, WAL truncated")
            await conn.commit()
    except Exception:
        log.warning("db: WAL checkpoint failed (DB may be held by another process)", exc_info=True)


async def get_db() -> AsyncSession:
    async with _AsyncSessionLocal() as session:
        yield session
