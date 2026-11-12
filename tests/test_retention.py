from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models import Incident, MonitorCheck
from app.services.retention import purge_old_checks
from tests.conftest import make_monitor, make_user


async def test_old_checks_are_purged_in_batches_and_recent_kept(sessionmaker):
    user = await make_user(sessionmaker)
    m = await make_monitor(sessionmaker, user.id)
    now = datetime.now(UTC)
    async with sessionmaker() as s:
        for i in range(7):
            s.add(
                MonitorCheck(
                    monitor_id=m.id, outcome="healthy", checked_at=now - timedelta(days=31 + i)
                )
            )
        for i in range(3):
            s.add(
                MonitorCheck(
                    monitor_id=m.id, outcome="healthy", checked_at=now - timedelta(days=29, hours=i)
                )
            )
        s.add(
            Incident(
                monitor_id=m.id,
                status="resolved",
                failure_reason="old",
                started_at=now - timedelta(days=60),
                resolved_at=now - timedelta(days=59),
            )
        )
        await s.commit()

    deleted = await purge_old_checks(sessionmaker, now, retention_days=30, batch_size=3)
    assert deleted == 7
    async with sessionmaker() as s:
        left = (await s.execute(select(MonitorCheck))).scalars().all()
        assert len(left) == 3
        assert all(c.checked_at > now - timedelta(days=30) for c in left)
        assert (await s.execute(select(Incident))).scalars().all()  # incidents are kept


async def test_purge_is_idempotent(sessionmaker):
    now = datetime.now(UTC)
    assert await purge_old_checks(sessionmaker, now, 30) == 0
    assert await purge_old_checks(sessionmaker, now, 30) == 0
