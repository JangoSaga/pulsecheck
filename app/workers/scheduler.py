"""Scheduler process: finds due monitors and enqueues check jobs. Runs no HTTP checks.

APScheduler drives four jobs on the event loop:
* tick: claim due monitors and push jobs to Redis
* requeue: return jobs abandoned by dead workers to the queue
* retention: purge old check history
* heartbeat: liveness signal for the container healthcheck

Several scheduler processes can run at once: claiming uses FOR UPDATE SKIP LOCKED.
"""

import asyncio
import logging
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.db import get_engine, get_sessionmaker
from app.logging import configure_logging, new_correlation_id, set_correlation_id
from app.services.queue import CheckQueue, beat
from app.services.retention import purge_old_checks
from app.services.scheduling import claim_due_monitors
from app.workers.runtime import install_signal_handlers

log = logging.getLogger("app.scheduler")


def build_scheduler(settings: Settings, redis: Redis) -> AsyncIOScheduler:
    queue = CheckQueue(redis)
    sessionmaker = get_sessionmaker()

    async def tick() -> None:
        set_correlation_id(new_correlation_id())
        try:
            await claim_due_monitors(
                sessionmaker, queue.enqueue, datetime.now(UTC), settings.scheduler_batch_size
            )
        except Exception:
            log.exception("scheduler tick failed")

    async def requeue() -> None:
        try:
            moved = await queue.requeue_stale(settings.stale_job_seconds)
            if moved:
                log.warning("requeued stale jobs", extra={"count": moved})
        except Exception:
            log.exception("requeue failed")

    async def retention() -> None:
        set_correlation_id(new_correlation_id())
        try:
            await purge_old_checks(
                sessionmaker,
                datetime.now(UTC),
                settings.retention_days,
                settings.retention_batch_size,
            )
        except Exception:
            log.exception("retention failed")

    async def heartbeat() -> None:
        try:
            await beat(redis, "scheduler", settings.heartbeat_ttl_seconds)
        except Exception:
            log.exception("heartbeat failed")

    sched = AsyncIOScheduler(timezone="UTC")
    common = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 30}
    sched.add_job(tick, "interval", seconds=settings.scheduler_tick_seconds, id="tick", **common)
    sched.add_job(requeue, "interval", seconds=30, id="requeue", **common)
    sched.add_job(
        retention,
        "interval",
        seconds=settings.retention_interval_seconds,
        id="retention",
        next_run_time=datetime.now(UTC),
        **common,
    )
    sched.add_job(
        heartbeat, "interval", seconds=10, id="heartbeat", next_run_time=datetime.now(UTC), **common
    )
    return sched


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    redis = Redis.from_url(settings.redis_url)
    sched = build_scheduler(settings, redis)
    stop = asyncio.Event()
    install_signal_handlers(stop)
    sched.start()
    log.info("scheduler started")
    try:
        await stop.wait()
    finally:
        sched.shutdown(wait=False)
        await redis.aclose()
        await get_engine().dispose()
        log.info("scheduler stopped")


if __name__ == "__main__":
    asyncio.run(main())
