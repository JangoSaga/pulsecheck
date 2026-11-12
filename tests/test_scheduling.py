import asyncio
import json
from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.models import Monitor
from app.services.queue import CheckQueue, make_job
from app.services.scheduling import claim_due_monitors
from app.workers.check_worker import process_job
from tests.conftest import make_monitor, make_user


class Sink:
    def __init__(self):
        self.jobs: list[str] = []

    async def __call__(self, jobs):
        self.jobs.extend(jobs)


async def test_due_monitor_is_claimed_and_rescheduled(sessionmaker):
    user = await make_user(sessionmaker)
    now = datetime.now(UTC)
    m = await make_monitor(sessionmaker, user.id, interval_seconds=60, next_check_at=now)
    sink = Sink()
    ids = await claim_due_monitors(sessionmaker, sink, now)
    assert ids == [m.id]
    assert json.loads(sink.jobs[0])["monitor_id"] == str(m.id)
    async with sessionmaker() as s:
        cur = await s.get(Monitor, m.id)
    assert cur.next_check_at == now + timedelta(seconds=60)
    # Not due again until the interval passes.
    assert await claim_due_monitors(sessionmaker, sink, now + timedelta(seconds=30)) == []
    assert await claim_due_monitors(sessionmaker, sink, now + timedelta(seconds=61)) == [m.id]


async def test_future_monitor_not_claimed(sessionmaker):
    user = await make_user(sessionmaker)
    now = datetime.now(UTC)
    await make_monitor(sessionmaker, user.id, next_check_at=now + timedelta(seconds=10))
    assert await claim_due_monitors(sessionmaker, Sink(), now) == []


async def test_paused_monitor_not_claimed(sessionmaker):
    user = await make_user(sessionmaker)
    await make_monitor(sessionmaker, user.id, is_active=False)
    assert await claim_due_monitors(sessionmaker, Sink(), datetime.now(UTC)) == []


async def test_deleted_monitor_not_claimed(sessionmaker):
    user = await make_user(sessionmaker)
    m = await make_monitor(sessionmaker, user.id)
    async with sessionmaker() as s:
        await s.delete(await s.get(Monitor, m.id))
        await s.commit()
    assert await claim_due_monitors(sessionmaker, Sink(), datetime.now(UTC)) == []


async def test_batch_size_is_respected_and_oldest_first(sessionmaker):
    user = await make_user(sessionmaker)
    now = datetime.now(UTC)
    ms = [
        await make_monitor(sessionmaker, user.id, next_check_at=now - timedelta(seconds=i))
        for i in range(5)
    ]
    ids = await claim_due_monitors(sessionmaker, Sink(), now, batch_size=2)
    assert ids == [ms[4].id, ms[3].id]


async def test_concurrent_schedulers_never_claim_the_same_monitor(sessionmaker):
    user = await make_user(sessionmaker)
    now = datetime.now(UTC)
    for _ in range(40):
        await make_monitor(sessionmaker, user.id, next_check_at=now)
    sink = Sink()
    results = await asyncio.gather(
        *(claim_due_monitors(sessionmaker, sink, now, batch_size=7) for _ in range(8))
    )
    claimed = [i for r in results for i in r]
    assert len(claimed) == len(set(claimed))  # no monitor claimed twice
    jobs = [json.loads(j)["monitor_id"] for j in sink.jobs]
    assert len(jobs) == len(set(jobs))
    # Remaining ones are picked up by follow-up ticks: nothing is lost.
    while True:
        more = await claim_due_monitors(sessionmaker, sink, now, batch_size=7)
        if not more:
            break
        claimed.extend(more)
    assert len(set(claimed)) == 40


async def test_claim_rolls_back_when_enqueue_fails(sessionmaker):
    user = await make_user(sessionmaker)
    now = datetime.now(UTC)
    m = await make_monitor(sessionmaker, user.id, next_check_at=now)

    async def broken(jobs):
        raise ConnectionError("redis down")

    try:
        await claim_due_monitors(sessionmaker, broken, now)
    except ConnectionError:
        pass
    # Still due: a Redis outage delays checks but never silently drops them.
    assert await claim_due_monitors(sessionmaker, Sink(), now) == [m.id]


async def test_queue_roundtrip_and_ack(redis):
    q = CheckQueue(redis)
    job = make_job("00000000-0000-0000-0000-000000000001", "cid")
    await q.enqueue([job])
    taken = await q.take(block_seconds=1)
    assert taken == job
    assert await q.depth() == (0, 1)
    await q.ack(taken)
    assert await q.depth() == (0, 0)
    assert await q.take(block_seconds=1) is None


async def test_dead_worker_job_is_requeued(redis):
    q = CheckQueue(redis)
    job = make_job("00000000-0000-0000-0000-000000000001", "cid")
    await q.enqueue([job])
    await q.take(block_seconds=1)  # worker takes it and "dies" without ack
    assert await q.requeue_stale(older_than_seconds=180) == 0  # still fresh
    import time

    assert await q.requeue_stale(older_than_seconds=180, now=time.time() + 400) == 1
    assert await q.depth() == (1, 0)
    again = await q.take(block_seconds=1)
    assert json.loads(again)["monitor_id"] == "00000000-0000-0000-0000-000000000001"


async def test_inflight_lock_prevents_parallel_checks(redis):
    q = CheckQueue(redis)
    assert await q.acquire_inflight("m1", 30)
    assert not await q.acquire_inflight("m1", 30)
    await q.release_inflight("m1")
    assert await q.acquire_inflight("m1", 30)


async def test_worker_skips_paused_deleted_and_malformed(sessionmaker, redis, settings):
    user = await make_user(sessionmaker)
    paused = await make_monitor(sessionmaker, user.id, is_active=False)
    q = CheckQueue(redis)
    kw = {"sessionmaker": sessionmaker, "queue": q, "settings": settings}
    assert await process_job(make_job(paused.id, "c"), **kw) == "skipped"
    gone = make_job("00000000-0000-0000-0000-000000000009", "c")
    assert await process_job(gone, **kw) == "skipped"
    assert await process_job("not json", **kw) == "malformed"


async def test_worker_drops_duplicate_concurrent_job(sessionmaker, redis, settings, local_server):
    user = await make_user(sessionmaker)
    m = await make_monitor(
        sessionmaker, user.id, target_url=local_server.url("/delay/1"), timeout_seconds=5
    )
    q = CheckQueue(redis)
    kw = {"sessionmaker": sessionmaker, "queue": q, "settings": settings}
    outcomes = await asyncio.gather(
        process_job(make_job(m.id, "a"), **kw), process_job(make_job(m.id, "b"), **kw)
    )
    assert sorted(outcomes) == ["duplicate", "stored"]
    assert local_server.hits.count("/delay/1") == 1


async def test_settings_default_is_safe():
    assert Settings(_env_file=None).allow_private_targets is False
