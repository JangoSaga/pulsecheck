"""Retention: check history older than RETENTION_DAYS is deleted in bounded batches.

Incidents and notifications are kept (they are small and are the audit trail).
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import MonitorCheck

log = logging.getLogger(__name__)


async def purge_old_checks(
    sessionmaker: async_sessionmaker[AsyncSession],
    now: datetime,
    retention_days: int,
    batch_size: int = 5000,
) -> int:
    cutoff = now - timedelta(days=retention_days)
    total = 0
    async with sessionmaker() as session:
        while True:
            ids = (
                (
                    await session.execute(
                        select(MonitorCheck.id)
                        .where(MonitorCheck.checked_at < cutoff)
                        .limit(batch_size)
                    )
                )
                .scalars()
                .all()
            )
            if not ids:
                break
            await session.execute(delete(MonitorCheck).where(MonitorCheck.id.in_(ids)))
            await session.commit()
            total += len(ids)
    log.info("retention purge", extra={"deleted": total, "cutoff": cutoff.isoformat()})
    return total
