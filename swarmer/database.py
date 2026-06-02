import logging

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_engine = None
_AsyncSessionLocal: async_sessionmaker | None = None


def init_db(database_url: str) -> None:
    global _engine, _AsyncSessionLocal
    pool_kwargs = {}
    if database_url.startswith("sqlite"):
        # NullPool: each session gets its own connection opened fresh and
        # released immediately on close — eliminates concurrent-write lock
        # contention between the scheduler, log pollers, and HTTP handlers.
        pool_kwargs["poolclass"] = NullPool
    _engine = create_async_engine(database_url, echo=False, **pool_kwargs)
    if database_url.startswith("sqlite"):
        # Set pragmas on every new connection (outside any transaction).
        # WAL mode allows concurrent readers alongside a writer.
        # busy_timeout=30000 ms makes SQLite retry writes for up to 30 s
        # instead of immediately raising "database is locked".
        @event.listens_for(_engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

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


async def get_db() -> AsyncSession:
    async with _AsyncSessionLocal() as session:
        yield session
