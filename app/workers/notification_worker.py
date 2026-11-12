"""Notification worker process: delivers pending incident emails with retry."""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from redis.asyncio import Redis

from app.config import get_settings
from app.db import get_engine, get_sessionmaker
from app.logging import configure_logging, new_correlation_id, set_correlation_id
from app.services.notifier import deliver_due_notifications, smtp_sender
from app.services.queue import beat
from app.workers.runtime import install_signal_handlers

log = logging.getLogger("app.notification_worker")


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    redis = Redis.from_url(settings.redis_url)
    sessionmaker = get_sessionmaker()
    sender = smtp_sender(settings)
    stop = asyncio.Event()
    install_signal_handlers(stop)
    log.info("notification worker started")
    last_beat = 0.0
    loop = asyncio.get_running_loop()
    try:
        while not stop.is_set():
            if loop.time() - last_beat >= 10:
                try:
                    await beat(redis, "notification-worker", settings.heartbeat_ttl_seconds)
                    last_beat = loop.time()
                except Exception:
                    log.exception("heartbeat failed")
            set_correlation_id(new_correlation_id())
            try:
                attempted = await deliver_due_notifications(
                    sessionmaker,
                    sender,
                    datetime.now(UTC),
                    max_attempts=settings.notify_max_attempts,
                    retry_base_seconds=settings.notify_retry_base_seconds,
                )
            except Exception:
                log.exception("notification loop failed")
                attempted = 0
            if attempted == 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=settings.notify_poll_seconds)
    finally:
        await redis.aclose()
        await get_engine().dispose()
        log.info("notification worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
