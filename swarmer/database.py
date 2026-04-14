from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


_engine = None
_AsyncSessionLocal: async_sessionmaker | None = None


def init_db(database_url: str) -> None:
    global _engine, _AsyncSessionLocal
    _engine = create_async_engine(database_url, echo=False)
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
    ]
    async with _engine.begin() as conn:
        for stmt in migrations:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass  # column already exists


async def get_db() -> AsyncSession:
    async with _AsyncSessionLocal() as session:
        yield session
