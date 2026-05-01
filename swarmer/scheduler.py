import asyncio
import logging
from datetime import datetime

from croniter import croniter
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

log = logging.getLogger(__name__)

_POLL_INTERVAL = 30.0
_scheduler_task: asyncio.Task | None = None


def start_scheduler() -> None:
    global _scheduler_task
    stop_scheduler()
    _scheduler_task = asyncio.create_task(
        _scheduler_loop(),
        name="cron-scheduler",
    )


def stop_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
    _scheduler_task = None


async def shutdown() -> None:
    task = _scheduler_task
    stop_scheduler()
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
