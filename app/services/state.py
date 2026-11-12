"""Persist a check result, update monitor state, open or resolve incidents.

Everything for one result happens in one transaction while holding a row lock on the
monitor (SELECT ... FOR UPDATE). Concurrent results for the same monitor therefore
serialize, which makes the threshold counters exact and incident opening race free.
A partial unique index on incidents is the backstop against duplicate open incidents.
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Incident, Monitor, MonitorCheck, Notification
from app.services.checker import CheckResult

log = logging.getLogger(__name__)

CHANNEL_EMAIL = "email"


async def _enqueue_notification(session: AsyncSession, incident_id: uuid.UUID, event: str) -> None:
    # ON CONFLICT DO NOTHING on (incident_id, channel, event): one per state transition.
    stmt = (
        pg_insert(Notification)
        .values(
            id=uuid.uuid4(),
            incident_id=incident_id,
            channel=CHANNEL_EMAIL,
            event=event,
            status="pending",
            attempt_count=0,
        )
        .on_conflict_do_nothing(constraint="uq_notification_transition")
    )
    await session.execute(stmt)


async def record_result(
    session: AsyncSession, monitor_id: uuid.UUID, result: CheckResult
) -> MonitorCheck | None:
    """Apply a result. Returns the stored check, or None if the monitor is gone or paused.

    Results for paused or deleted monitors are discarded so a check that was in flight
    when the user paused or deleted cannot reopen incidents or send mail.
    """
    monitor = (
        await session.execute(select(Monitor).where(Monitor.id == monitor_id).with_for_update())
    ).scalar_one_or_none()
    if monitor is None or not monitor.is_active:
        await session.rollback()
        return None

    check = MonitorCheck(
        monitor_id=monitor.id,
        outcome=result.outcome,
        status_code=result.status_code,
        response_time_ms=result.response_time_ms,
        error_type=result.error_type,
        error_message=result.error_message,
        checked_at=result.checked_at,
    )
    session.add(check)

    open_incident = (
        await session.execute(
            select(Incident).where(Incident.monitor_id == monitor.id, Incident.status == "open")
        )
    ).scalar_one_or_none()

    if result.healthy:
        monitor.consecutive_successes += 1
        monitor.consecutive_failures = 0
    else:
        monitor.consecutive_failures += 1
        monitor.consecutive_successes = 0
    monitor.last_checked_at = result.checked_at

    if not result.healthy and open_incident is None:
        if monitor.consecutive_failures >= monitor.failure_threshold:
            incident = Incident(
                monitor_id=monitor.id,
                status="open",
                started_at=result.checked_at,
                failure_reason=result.failure_reason[:255],
            )
            session.add(incident)
            await session.flush()
            await _enqueue_notification(session, incident.id, "opened")
            monitor.status = "down"
            log.info(
                "incident opened",
                extra={"monitor_id": str(monitor.id), "incident_id": str(incident.id)},
            )
    elif result.healthy and open_incident is not None:
        if monitor.consecutive_successes >= monitor.recovery_threshold:
            open_incident.status = "resolved"
            open_incident.resolved_at = result.checked_at
            await _enqueue_notification(session, open_incident.id, "resolved")
            monitor.status = "up"
            log.info(
                "incident resolved",
                extra={"monitor_id": str(monitor.id), "incident_id": str(open_incident.id)},
            )
    elif result.healthy and open_incident is None:
        monitor.status = "up"
    # Unhealthy below the threshold, or healthy but still inside the recovery window:
    # the reported status is deliberately left unchanged.

    await session.commit()
    return check
