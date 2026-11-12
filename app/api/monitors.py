import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.deps import (
    CurrentUser,
    OwnedMonitor,
    PageDep,
    RangeDep,
    ResolverDep,
    SessionDep,
    SettingsDep,
    get_owned_monitor,
)
from app.models import Incident, Monitor, MonitorCheck
from app.schemas import (
    CheckOut,
    IncidentOut,
    MonitorCreate,
    MonitorOut,
    MonitorUpdate,
    Page,
)
from app.services.ssrf import InvalidTargetError, Resolver, UnsafeTargetError, validate_target_url

router = APIRouter(prefix="/api/v1/monitors", tags=["monitors"])


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail)


async def _validate(
    url: str,
    interval: int,
    timeout_s: int,
    settings: Settings,
    resolver: Resolver | None,
) -> None:
    if interval < settings.min_interval_seconds:
        raise _unprocessable(f"interval_seconds must be at least {settings.min_interval_seconds}")
    if interval > settings.max_interval_seconds:
        raise _unprocessable(f"interval_seconds must be at most {settings.max_interval_seconds}")
    if timeout_s > settings.max_timeout_seconds:
        raise _unprocessable(f"timeout_seconds must be at most {settings.max_timeout_seconds}")
    if timeout_s > interval:
        raise _unprocessable("timeout_seconds must not exceed interval_seconds")
    try:
        await validate_target_url(
            url, allow_private=settings.allow_private_targets, resolver=resolver
        )
    except InvalidTargetError as exc:
        raise _unprocessable(f"Invalid target_url: {exc}") from exc
    except UnsafeTargetError as exc:
        raise _unprocessable(f"Unsafe target_url: {exc}") from exc


@router.post("", response_model=MonitorOut, status_code=status.HTTP_201_CREATED)
async def create_monitor(
    body: MonitorCreate,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    resolver: ResolverDep,
) -> Monitor:
    count = await session.scalar(
        select(func.count()).select_from(Monitor).where(Monitor.user_id == user.id)
    )
    if (count or 0) >= settings.max_monitors_per_user:
        raise HTTPException(status.HTTP_409_CONFLICT, "Monitor limit reached")
    await _validate(
        body.target_url, body.interval_seconds, body.timeout_seconds, settings, resolver
    )
    monitor = Monitor(
        user_id=user.id,
        **body.model_dump(),
        next_check_at=datetime.now(UTC),  # first check as soon as a scheduler tick sees it
    )
    session.add(monitor)
    await session.commit()
    return monitor


@router.get("", response_model=Page[MonitorOut])
async def list_monitors(user: CurrentUser, session: SessionDep, page: PageDep) -> Page[Monitor]:
    total = await session.scalar(
        select(func.count()).select_from(Monitor).where(Monitor.user_id == user.id)
    )
    items = (
        (
            await session.execute(
                select(Monitor)
                .where(Monitor.user_id == user.id)
                .order_by(Monitor.created_at.desc(), Monitor.id)
                .limit(page.limit)
                .offset(page.offset)
            )
        )
        .scalars()
        .all()
    )
    return Page(items=list(items), total=total or 0, limit=page.limit, offset=page.offset)


@router.get("/{monitor_id}", response_model=MonitorOut)
async def get_monitor(monitor: OwnedMonitor) -> Monitor:
    return monitor


@router.patch("/{monitor_id}", response_model=MonitorOut)
async def update_monitor(
    body: MonitorUpdate,
    monitor_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    resolver: ResolverDep,
) -> Monitor:
    monitor = await get_owned_monitor(monitor_id, user, session, lock=True)
    changes = body.model_dump(exclude_unset=True)
    for field in ("name", "target_url", "http_method", "interval_seconds", "timeout_seconds"):
        if field in changes and changes[field] is None:
            raise _unprocessable(f"{field} cannot be null")
    merged_url = changes.get("target_url", monitor.target_url)
    interval = changes.get("interval_seconds", monitor.interval_seconds)
    timeout = changes.get("timeout_seconds", monitor.timeout_seconds)
    await _validate(merged_url, interval, timeout, settings, resolver)

    reschedule = any(k in changes for k in ("target_url", "http_method", "interval_seconds"))
    for key, value in changes.items():
        if value is None:
            continue
        setattr(monitor, key, value)
    if reschedule and monitor.is_active:
        monitor.next_check_at = datetime.now(UTC)
    await session.commit()
    return monitor


@router.delete("/{monitor_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_monitor(monitor_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> Response:
    monitor = await get_owned_monitor(monitor_id, user, session, lock=True)
    await session.delete(monitor)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{monitor_id}/pause", response_model=MonitorOut)
async def pause_monitor(monitor_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> Monitor:
    monitor = await get_owned_monitor(monitor_id, user, session, lock=True)
    monitor.is_active = False
    await session.commit()
    return monitor


@router.post("/{monitor_id}/resume", response_model=MonitorOut)
async def resume_monitor(monitor_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> Monitor:
    monitor = await get_owned_monitor(monitor_id, user, session, lock=True)
    if not monitor.is_active:
        monitor.is_active = True
        monitor.next_check_at = datetime.now(UTC)
        # A long pause makes old streaks meaningless.
        monitor.consecutive_failures = 0
        monitor.consecutive_successes = 0
    await session.commit()
    return monitor


@router.get("/{monitor_id}/checks", response_model=Page[CheckOut])
async def list_checks(
    monitor: OwnedMonitor,
    session: SessionDep,
    page: PageDep,
    rng: RangeDep,
    outcome: Annotated[Literal["healthy", "unhealthy"] | None, Query()] = None,
) -> Page[MonitorCheck]:
    conds = [
        MonitorCheck.monitor_id == monitor.id,
        MonitorCheck.checked_at >= rng.start,
        MonitorCheck.checked_at < rng.end,
    ]
    if outcome:
        conds.append(MonitorCheck.outcome == outcome)
    total = await session.scalar(select(func.count()).select_from(MonitorCheck).where(*conds))
    items = (
        (
            await session.execute(
                select(MonitorCheck)
                .where(*conds)
                .order_by(MonitorCheck.checked_at.desc(), MonitorCheck.id)
                .limit(page.limit)
                .offset(page.offset)
            )
        )
        .scalars()
        .all()
    )
    return Page(items=list(items), total=total or 0, limit=page.limit, offset=page.offset)


@router.get("/{monitor_id}/incidents", response_model=Page[IncidentOut])
async def list_incidents(
    monitor: OwnedMonitor,
    session: SessionDep,
    page: PageDep,
    rng: RangeDep,
    incident_status: Annotated[Literal["open", "resolved"] | None, Query(alias="status")] = None,
) -> Page[Incident]:
    conds = [
        Incident.monitor_id == monitor.id,
        Incident.started_at >= rng.start,
        Incident.started_at < rng.end,
    ]
    if incident_status:
        conds.append(Incident.status == incident_status)
    total = await session.scalar(select(func.count()).select_from(Incident).where(*conds))
    items = (
        (
            await session.execute(
                select(Incident)
                .where(*conds)
                .options(selectinload(Incident.notifications))
                .order_by(Incident.started_at.desc(), Incident.id)
                .limit(page.limit)
                .offset(page.offset)
            )
        )
        .scalars()
        .all()
    )
    return Page(items=list(items), total=total or 0, limit=page.limit, offset=page.offset)
