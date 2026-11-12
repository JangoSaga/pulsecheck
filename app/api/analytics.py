import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.deps import CurrentUser, RangeDep, SessionDep, get_owned_monitor
from app.models import Monitor
from app.schemas import UptimeOut, UptimeResponse
from app.services.analytics import compute_uptime

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


@router.get("/uptime", response_model=UptimeResponse)
async def uptime(
    user: CurrentUser,
    session: SessionDep,
    rng: RangeDep,
    monitor_id: Annotated[uuid.UUID | None, Query()] = None,
) -> UptimeResponse:
    """Uptime for one monitor (monitor_id) or for all of the caller's monitors."""
    if monitor_id is not None:
        monitors = [await get_owned_monitor(monitor_id, user, session)]
    else:
        monitors = list(
            (
                await session.execute(
                    select(Monitor).where(Monitor.user_id == user.id).order_by(Monitor.created_at)
                )
            )
            .scalars()
            .all()
        )
    now = datetime.now(UTC)
    stats = [await compute_uptime(session, m, rng.start, rng.end, now) for m in monitors]
    return UptimeResponse(
        start=rng.start,
        end=rng.end,
        monitors=[UptimeOut(**s.__dict__) for s in stats],
    )
