import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.models import Monitor, User
from app.security import decode_access_token
from app.services.ssrf import Resolver

MAX_RANGE = timedelta(days=90)
DEFAULT_RANGE = timedelta(hours=24)

bearer = HTTPBearer(auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_resolver() -> Resolver | None:
    """Override point: tests inject a fake DNS resolver. None means the system resolver."""
    return None


ResolverDep = Annotated[Resolver | None, Depends(get_resolver)]


async def get_current_user(
    session: SessionDep,
    settings: SettingsDep,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> User:
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if creds is None:
        raise unauthorized
    user_id = decode_access_token(creds.credentials, settings)
    if user_id is None:
        raise unauthorized
    user = await session.get(User, user_id)
    if user is None:
        raise unauthorized
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_owned_monitor(
    monitor_id: uuid.UUID, user: CurrentUser, session: SessionDep, lock: bool = False
) -> Monitor:
    stmt = select(Monitor).where(Monitor.id == monitor_id, Monitor.user_id == user.id)
    if lock:
        stmt = stmt.with_for_update()
    monitor = (await session.execute(stmt)).scalar_one_or_none()
    if monitor is None:
        # 404 for both "missing" and "not yours": do not reveal other users' monitor ids.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Monitor not found")
    return monitor


OwnedMonitor = Annotated[Monitor, Depends(get_owned_monitor)]


@dataclass
class Pagination:
    limit: int
    offset: int


def pagination(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> Pagination:
    return Pagination(limit, offset)


PageDep = Annotated[Pagination, Depends(pagination)]


@dataclass
class DateRange:
    start: datetime
    end: datetime


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def date_range(
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
) -> DateRange:
    end = _aware(to) if to else datetime.now(UTC)
    start = _aware(from_) if from_ else end - DEFAULT_RANGE
    if start >= end:
        raise HTTPException(422, "'from' must be before 'to'")
    if end - start > MAX_RANGE:
        raise HTTPException(
            422,
            f"Date range must not exceed {MAX_RANGE.days} days",
        )
    return DateRange(start, end)


RangeDep = Annotated[DateRange, Depends(date_range)]
