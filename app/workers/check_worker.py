"""Check worker process: takes jobs from Redis, runs the HTTP check, persists the result.

Concurrency is bounded by a semaphore (MAX_CONCURRENT_CHECKS). The worker only takes a
job from the queue once it has a free slot, so backlog stays in Redis, not in memory.
"""

import asyncio
import json
import logging
import uuid

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db import get_engine, get_sessionmaker
from app.logging import configure_logging, set_correlation_id
from app.models import Monitor
from app.services.checker import perform_check
from app.services.queue import CheckQueue, beat
from app.services.ssrf import Resolver
from app.services.state import record_result
from app.workers.runtime import install_signal_handlers

log = logging.getLogger("app.check_worker")


async def process_job(
    raw: str,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    queue: CheckQueue,
    settings: Settings,
    resolver: Resolver | None = None,
    transport=None,
) -> str:
    """Run one job. Always acks. Returns a short outcome label (useful in tests)."""
    try:
        job = json.loads(raw)
        monitor_id = uuid.UUID(job["monitor_id"])
    except (ValueError, KeyError, TypeError):
        log.error("discarding malformed job")
        await queue.ack(raw)
        return "malformed"
    set_correlation_id(job.get("correlation_id"))
    key = str(monitor_id)

    try:
        async with sessionmaker() as session:
            monitor = await session.get(Monitor, monitor_id)
            if monitor is None or not monitor.is_active:
                return "skipped"
            url, method = monitor.target_url, monitor.http_method
            timeout, expected = monitor.timeout_seconds, list(monitor.expected_status_codes)

        if not await queue.acquire_inflight(key, timeout + 15):
            log.info("duplicate job dropped", extra={"monitor_id": key})
            return "duplicate"
        try:
            result = await perform_check(
                url,
                method,
                timeout,
                expected,
                allow_private=settings.allow_private_targets,
                resolver=resolver,
                transport=transport,
            )
            async with sessionmaker() as session:
                stored = await record_result(session, monitor_id, result)
            log.info(
                "check completed",
                extra={
                    "monitor_id": key,
                    "outcome": result.outcome,
                    "status_code": result.status_code,
                    "response_time_ms": result.response_time_ms,
                    "error_type": result.error_type,
                    "stored": stored is not None,
                },
            )
            return "stored" if stored else "discarded"
        finally:
            await queue.release_inflight(key)
    except Exception:
        log.exception("check job failed", extra={"monitor_id": key})
        return "error"
    finally:
        await queue.ack(raw)
        set_correlation_id(None)


async def run_worker(settings: Settings, redis: Redis, stop: asyncio.Event) -> None:
    queue = CheckQueue(redis)
    sessionmaker = get_sessionmaker()
    sem = asyncio.Semaphore(settings.max_concurrent_checks)
    tasks: set[asyncio.Task] = set()

    async def heartbeat() -> None:
        while not stop.is_set():
            try:
                await beat(redis, "check-worker", settings.heartbeat_ttl_seconds)
            except Exception:
                log.exception("heartbeat failed")
            await asyncio.sleep(10)

    hb = asyncio.create_task(heartbeat())

    async def run(raw: str) -> None:
        try:
            await process_job(raw, sessionmaker=sessionmaker, queue=queue, settings=settings)
        finally:
            sem.release()

    while not stop.is_set():
        await sem.acquire()
        try:
            raw = await queue.take(block_seconds=2)
        except Exception:
            sem.release()
            log.exception("queue take failed")
            await asyncio.sleep(1)
            continue
        if raw is None:
            sem.release()
            continue
        task = asyncio.create_task(run(raw))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    # Graceful shutdown: let in-flight checks finish (bounded by their own timeouts).
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    hb.cancel()


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    redis = Redis.from_url(settings.redis_url)
    stop = asyncio.Event()
    install_signal_handlers(stop)
    log.info("check worker started", extra={"max_concurrent": settings.max_concurrent_checks})
    try:
        await run_worker(settings, redis, stop)
    finally:
        await redis.aclose()
        await get_engine().dispose()
        log.info("check worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
