"""Claiming due monitors.

The claim is a single transaction:

    SELECT ... FROM monitors WHERE is_active AND next_check_at <= now
    ORDER BY next_check_at LIMIT n FOR UPDATE SKIP LOCKED

then next_check_at is advanced by the interval, the jobs are pushed to the queue, and
the transaction commits. SKIP LOCKED means concurrent schedulers never claim the same
monitor. The push happens before commit: if the commit fails the monitor stays due and
may be queued twice (at-least-once), never zero times.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.logging import new_correlation_id
from app.models import Monitor
from app.services.queue import make_job

log = logging.getLogger(__name__)

Enqueue = Callable[[list[str]], Awaitable[None]]


async def claim_due_monitors(
    sessionmaker: async_sessionmaker[AsyncSession],
    enqueue: Enqueue,
    now: datetime,
    batch_size: int = 200,
) -> list[uuid.UUID]:
    async with sessionmaker() as session:
        monitors = (
            (
                await session.execute(
                    select(Monitor)
                    .where(Monitor.is_active.is_(True), Monitor.next_check_at <= now)
                    .order_by(Monitor.next_check_at)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        if not monitors:
            return []
        jobs = []
        for m in monitors:
            # Advance from "now", not from the old due time, so a long outage of the
            # scheduler does not cause a burst of catch-up checks.
            m.next_check_at = now + timedelta(seconds=m.interval_seconds)
            jobs.append(make_job(m.id, new_correlation_id()))
        await enqueue(jobs)
        ids = [m.id for m in monitors]
        await session.commit()
    log.info("monitors claimed", extra={"count": len(ids)})
    return ids
