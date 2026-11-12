"""Uptime analytics. The formulas are documented in README.md ("Uptime calculation").

uptime_percentage = successful_checks / total_checks * 100 over checks whose checked_at
is in [start, end). Missing checks (paused periods, outages of PulseCheck itself) are
neither successes nor failures: they are excluded from the ratio and reported through
``coverage_percentage`` so the reader can judge how complete the data is.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Float, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Incident, Monitor, MonitorCheck


@dataclass
class UptimeStats:
    monitor_id: uuid.UUID
    monitor_name: str
    current_status: str
    is_active: bool
    start: datetime
    end: datetime
    total_checks: int
    successful_checks: int
    failed_checks: int
    uptime_percentage: float | None
    expected_checks: int
    coverage_percentage: float | None
    avg_response_time_ms: float | None
    p50_response_time_ms: float | None
    p95_response_time_ms: float | None
    p99_response_time_ms: float | None
    incident_count: int
    total_incident_seconds: float


async def compute_uptime(
    session: AsyncSession, monitor: Monitor, start: datetime, end: datetime, now: datetime
) -> UptimeStats:
    healthy = case((MonitorCheck.outcome == "healthy", 1), else_=0)
    row = (
        await session.execute(
            select(
                func.count(MonitorCheck.id),
                func.coalesce(func.sum(healthy), 0),
                func.avg(MonitorCheck.response_time_ms),
                func.percentile_cont(0.5).within_group(MonitorCheck.response_time_ms),
                func.percentile_cont(0.95).within_group(MonitorCheck.response_time_ms),
                func.percentile_cont(0.99).within_group(MonitorCheck.response_time_ms),
            ).where(
                MonitorCheck.monitor_id == monitor.id,
                MonitorCheck.checked_at >= start,
                MonitorCheck.checked_at < end,
            )
        )
    ).one()
    total, ok, avg, p50, p95, p99 = row
    total = int(total)
    ok = int(ok)

    # Incidents overlapping the window; duration is clipped to the window.
    resolved_or_now = func.coalesce(Incident.resolved_at, now)
    overlap_end = func.least(resolved_or_now, end)
    overlap_start = func.greatest(Incident.started_at, start)
    inc = (
        await session.execute(
            select(
                func.count(Incident.id),
                func.coalesce(
                    func.sum(cast(func.extract("epoch", overlap_end - overlap_start), Float)), 0.0
                ),
            ).where(
                Incident.monitor_id == monitor.id,
                Incident.started_at < end,
                resolved_or_now > start,
            )
        )
    ).one()

    # Expected checks: the window clipped to the monitor's lifetime, divided by interval.
    effective_start = max(start, monitor.created_at)
    effective_end = min(end, now)
    span = max((effective_end - effective_start).total_seconds(), 0.0)
    expected = int(span // monitor.interval_seconds)
    coverage = round(min(total / expected, 1.0) * 100, 2) if expected > 0 else None

    return UptimeStats(
        monitor_id=monitor.id,
        monitor_name=monitor.name,
        current_status=monitor.status if monitor.is_active else "paused",
        is_active=monitor.is_active,
        start=start,
        end=end,
        total_checks=total,
        successful_checks=ok,
        failed_checks=total - ok,
        uptime_percentage=round(ok / total * 100, 4) if total else None,
        expected_checks=expected,
        coverage_percentage=coverage,
        avg_response_time_ms=round(float(avg), 2) if avg is not None else None,
        p50_response_time_ms=round(float(p50), 2) if p50 is not None else None,
        p95_response_time_ms=round(float(p95), 2) if p95 is not None else None,
        p99_response_time_ms=round(float(p99), 2) if p99 is not None else None,
        incident_count=int(inc[0]),
        total_incident_seconds=round(float(inc[1]), 2),
    )
