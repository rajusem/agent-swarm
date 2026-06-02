import asyncio
import logging
from datetime import datetime

from croniter import croniter
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

log = logging.getLogger(__name__)

_POLL_INTERVAL = 30.0

_scheduler_task: asyncio.Task | None = None
_gc_task: asyncio.Task | None = None


def start_scheduler() -> None:
    global _scheduler_task, _gc_task
    stop_scheduler()
    _scheduler_task = asyncio.create_task(
        _scheduler_loop(),
        name="cron-scheduler",
    )
    _gc_task = asyncio.create_task(
        _sandbox_gc_loop(),
        name="sandbox-gc",
    )


def stop_scheduler() -> None:
    global _scheduler_task, _gc_task
    for task in (_scheduler_task, _gc_task):
        if task and not task.done():
            task.cancel()
    _scheduler_task = None
    _gc_task = None


async def shutdown() -> None:
    tasks = [_scheduler_task, _gc_task]
    stop_scheduler()
    for task in tasks:
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass


async def _scheduler_loop() -> None:
    log.warning("scheduler: started, polling every %ds", int(_POLL_INTERVAL))
    try:
        while True:
            await asyncio.sleep(_POLL_INTERVAL)
            try:
                await _check_and_launch()
            except Exception:
                log.exception("scheduler: error in check_and_launch cycle")
    except asyncio.CancelledError:
        raise


async def _sandbox_gc_loop() -> None:
    from swarmer.config import settings
    interval = settings.sandbox_gc_interval
    log.warning("sandbox-gc: started, running every %ds", interval)
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await _collect_orphaned_sandboxes()
            except Exception:
                log.exception("sandbox-gc: error in collection cycle")
    except asyncio.CancelledError:
        raise


async def _collect_orphaned_sandboxes() -> None:
    """Delete OpenShell sandboxes that have no corresponding active DB session."""
    from swarmer.config import settings
    from swarmer.database import get_db
    from swarmer.models.session import Session

    if not settings.openshell_enabled:
        return

    try:
        from swarmer.openshell_client import _get_client
        client = _get_client()
        live_sandboxes = client.list(limit=200)
    except Exception:
        log.exception("sandbox-gc: failed to list sandboxes from gateway")
        return

    if not live_sandboxes:
        return

    live_names = {s.name for s in live_sandboxes}

    # Find which sandbox names belong to active sessions in the DB.
    async for db in get_db():
        result = await db.execute(
            select(Session.sandbox_name).where(
                Session.sandbox_name.isnot(None),
                Session.phase.in_(["pending", "running"]),
            )
        )
        active_names = {row[0] for row in result.fetchall() if row[0]}
        break

    orphans = live_names - active_names
    if not orphans:
        return

    log.warning("sandbox-gc: found %d orphaned sandbox(es): %s", len(orphans), orphans)
    for name in orphans:
        try:
            client.delete(name)
            log.warning("sandbox-gc: deleted orphaned sandbox %r", name)
        except Exception:
            log.exception("sandbox-gc: failed to delete sandbox %r", name)


async def _check_and_launch() -> None:
    from swarmer.database import get_db
    from swarmer.models.session import Session

    now = datetime.utcnow()

    async for db in get_db():
        # Atomically claim due sessions by setting phase='pending'.
        # The UPDATE's WHERE predicates ensure only unclaimed rows are
        # touched — this is DB-agnostic (works on both SQLite and Postgres).
        claim_result = await db.execute(
            update(Session)
            .where(
                Session.cron_schedule != "",
                Session.cron_next_run <= now,
                Session.mode == "prompt",
                Session.phase.notin_(["pending", "running"]),
            )
            .values(phase="pending")
            .returning(Session.id)
        )
        claimed_ids = [row[0] for row in claim_result.fetchall()]
        if not claimed_ids:
            break
        await db.commit()

        # Load the claimed sessions with relationships for processing.
        result = await db.execute(
            select(Session)
            .where(Session.id.in_(claimed_ids))
            .options(
                selectinload(Session.workspace),
                selectinload(Session.github_pat),
                selectinload(Session.repos),
            )
        )
        due_sessions = result.scalars().all()

        for session in due_sessions:
            ws = session.workspace
            if ws is None:
                continue

            log.warning(
                "scheduler: launching session %d (%s), was due at %s",
                session.id, session.name, session.cron_next_run,
            )
            try:
                from swarmer.routers.sessions import _do_launch
                await _do_launch(session, ws, db)

                session.cron_next_run = croniter(
                    session.cron_schedule, datetime.utcnow()
                ).get_next(datetime)
                await db.commit()

                log.warning(
                    "scheduler: session %d launched, next run at %s",
                    session.id, session.cron_next_run,
                )
            except Exception:
                log.exception("scheduler: failed to launch session %d", session.id)
                await db.rollback()
                session.phase = "idle"
                session.cron_next_run = croniter(
                    session.cron_schedule, datetime.utcnow()
                ).get_next(datetime)
                await db.commit()
        break
