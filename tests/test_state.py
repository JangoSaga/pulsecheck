import asyncio
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.models import Incident, Monitor, MonitorCheck, Notification
from app.services.state import record_result
from tests.conftest import make_monitor, make_user, result


async def apply(sessionmaker, monitor_id, healthy, **kw):
    async with sessionmaker() as s:
        return await record_result(s, monitor_id, result(healthy, **kw))


async def incidents(sessionmaker, monitor_id):
    async with sessionmaker() as s:
        return (
            (await s.execute(select(Incident).where(Incident.monitor_id == monitor_id)))
            .scalars()
            .all()
        )


async def state(sessionmaker, monitor_id) -> Monitor:
    async with sessionmaker() as s:
        return await s.get(Monitor, monitor_id)


async def test_result_is_persisted_and_state_updated(sessionmaker):
    user = await make_user(sessionmaker)
    m = await make_monitor(sessionmaker, user.id)
    check = await apply(sessionmaker, m.id, True)
    assert check is not None
    cur = await state(sessionmaker, m.id)
    assert cur.status == "up"
    assert cur.consecutive_successes == 1 and cur.consecutive_failures == 0
    assert cur.last_checked_at is not None
    async with sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(MonitorCheck)) == 1


async def test_single_failure_does_not_open_incident(sessionmaker):
    user = await make_user(sessionmaker)
    m = await make_monitor(sessionmaker, user.id, failure_threshold=3)
    await apply(sessionmaker, m.id, False)
    await apply(sessionmaker, m.id, False)
    assert await incidents(sessionmaker, m.id) == []
    assert (await state(sessionmaker, m.id)).status == "unknown"


async def test_incident_opens_exactly_at_failure_threshold(sessionmaker):
    user = await make_user(sessionmaker)
    m = await make_monitor(sessionmaker, user.id, failure_threshold=3)
    for _ in range(3):
        await apply(sessionmaker, m.id, False)
    incs = await incidents(sessionmaker, m.id)
    assert len(incs) == 1
    assert incs[0].status == "open" and incs[0].resolved_at is None
    assert "500" in incs[0].failure_reason
    assert (await state(sessionmaker, m.id)).status == "down"


async def test_transient_failure_resets_by_a_success(sessionmaker):
    user = await make_user(sessionmaker)
    m = await make_monitor(sessionmaker, user.id, failure_threshold=3)
    for healthy in (False, False, True, False, False):
        await apply(sessionmaker, m.id, healthy)
    assert await incidents(sessionmaker, m.id) == []


async def test_no_duplicate_incident_while_failing(sessionmaker):
    user = await make_user(sessionmaker)
    m = await make_monitor(sessionmaker, user.id, failure_threshold=2)
    for _ in range(10):
        await apply(sessionmaker, m.id, False)
    assert len(await incidents(sessionmaker, m.id)) == 1
    async with sessionmaker() as s:
        n = await s.scalar(select(func.count()).select_from(Notification))
        assert n == 1


async def test_incident_resolves_at_recovery_threshold(sessionmaker):
    user = await make_user(sessionmaker)
    m = await make_monitor(sessionmaker, user.id, failure_threshold=2, recovery_threshold=3)
    for _ in range(2):
        await apply(sessionmaker, m.id, False)
    await apply(sessionmaker, m.id, True)
    await apply(sessionmaker, m.id, True)
    inc = (await incidents(sessionmaker, m.id))[0]
    assert inc.status == "open"  # 2 of 3 successes
    assert (await state(sessionmaker, m.id)).status == "down"
    await apply(sessionmaker, m.id, True)
    inc = (await incidents(sessionmaker, m.id))[0]
    assert inc.status == "resolved" and inc.resolved_at is not None
    assert (await state(sessionmaker, m.id)).status == "up"
    async with sessionmaker() as s:
        events = sorted((await s.execute(select(Notification.event))).scalars().all())
    assert events == ["opened", "resolved"]


async def test_failure_during_recovery_window_resets_streak(sessionmaker):
    user = await make_user(sessionmaker)
    m = await make_monitor(sessionmaker, user.id, failure_threshold=1, recovery_threshold=2)
    await apply(sessionmaker, m.id, False)
    await apply(sessionmaker, m.id, True)
    await apply(sessionmaker, m.id, False)
    await apply(sessionmaker, m.id, True)
    assert (await incidents(sessionmaker, m.id))[0].status == "open"
    await apply(sessionmaker, m.id, True)
    assert (await incidents(sessionmaker, m.id))[0].status == "resolved"


async def test_new_incident_after_resolution(sessionmaker):
    user = await make_user(sessionmaker)
    m = await make_monitor(sessionmaker, user.id, failure_threshold=1, recovery_threshold=1)
    for healthy in (False, True, False, True):
        await apply(sessionmaker, m.id, healthy)
    incs = await incidents(sessionmaker, m.id)
    assert len(incs) == 2 and all(i.status == "resolved" for i in incs)


async def test_concurrent_results_never_duplicate_incident(sessionmaker):
    user = await make_user(sessionmaker)
    m = await make_monitor(sessionmaker, user.id, failure_threshold=3)
    await asyncio.gather(*(apply(sessionmaker, m.id, False) for _ in range(12)))
    assert len(await incidents(sessionmaker, m.id)) == 1
    cur = await state(sessionmaker, m.id)
    assert cur.consecutive_failures == 12  # row lock makes the counter exact
    async with sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(Notification)) == 1


async def test_paused_monitor_result_is_discarded(sessionmaker):
    user = await make_user(sessionmaker)
    m = await make_monitor(sessionmaker, user.id, failure_threshold=1, is_active=False)
    assert await apply(sessionmaker, m.id, False) is None
    assert await incidents(sessionmaker, m.id) == []
    async with sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(MonitorCheck)) == 0


async def test_deleted_monitor_result_is_discarded(sessionmaker):
    user = await make_user(sessionmaker)
    m = await make_monitor(sessionmaker, user.id)
    async with sessionmaker() as s:
        await s.delete(await s.get(Monitor, m.id))
        await s.commit()
    assert await apply(sessionmaker, m.id, False) is None


async def test_incident_uses_check_timestamp(sessionmaker):
    user = await make_user(sessionmaker)
    m = await make_monitor(sessionmaker, user.id, failure_threshold=1)
    when = datetime(2026, 1, 1, tzinfo=UTC)
    await apply(sessionmaker, m.id, False, when=when)
    assert (await incidents(sessionmaker, m.id))[0].started_at == when
