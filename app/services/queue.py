"""Redis-backed reliable work queue for check jobs.

Jobs move queue -> processing with BLMOVE when a worker takes them and are removed from
processing when the worker finishes (ack). A worker that dies mid-check leaves the job in
processing; ``requeue_stale`` returns jobs older than the visibility timeout to the queue.
Delivery is at-least-once. Duplicates are harmless: workers hold a per-monitor in-flight
lock and results are applied under a monitor row lock.
"""

import json
import time
import uuid

from redis.asyncio import Redis

QUEUE_KEY = "pulsecheck:checks:queue"
PROCESSING_KEY = "pulsecheck:checks:processing"
INFLIGHT_PREFIX = "pulsecheck:checks:inflight:"
HEARTBEAT_PREFIX = "pulsecheck:heartbeat:"


def make_job(monitor_id: uuid.UUID | str, correlation_id: str) -> str:
    return json.dumps(
        {
            "monitor_id": str(monitor_id),
            "correlation_id": correlation_id,
            "enqueued_at": time.time(),
            "job_id": uuid.uuid4().hex,
        }
    )


class CheckQueue:
    def __init__(
        self, redis: Redis, queue_key: str = QUEUE_KEY, processing_key: str = PROCESSING_KEY
    ):
        self.redis = redis
        self.queue_key = queue_key
        self.processing_key = processing_key

    async def enqueue(self, jobs: list[str]) -> None:
        if jobs:
            await self.redis.lpush(self.queue_key, *jobs)

    async def take(self, block_seconds: int = 2) -> str | None:
        raw = await self.redis.blmove(
            self.queue_key, self.processing_key, block_seconds, "RIGHT", "LEFT"
        )
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else raw

    async def ack(self, raw: str) -> None:
        await self.redis.lrem(self.processing_key, 1, raw)

    async def depth(self) -> tuple[int, int]:
        return await self.redis.llen(self.queue_key), await self.redis.llen(self.processing_key)

    async def requeue_stale(self, older_than_seconds: float, now: float | None = None) -> int:
        """Return jobs stuck in processing longer than the visibility timeout."""
        now = now if now is not None else time.time()
        moved = 0
        for raw in await self.redis.lrange(self.processing_key, 0, -1):
            text = raw.decode() if isinstance(raw, bytes) else raw
            try:
                enqueued_at = float(json.loads(text).get("enqueued_at", 0))
            except (ValueError, TypeError, AttributeError):
                enqueued_at = 0
            if now - enqueued_at <= older_than_seconds:
                continue
            if await self.redis.lrem(self.processing_key, 1, text):
                # Refresh the timestamp so a requeued job is not immediately stale again.
                refreshed = json.loads(text) if enqueued_at else {}
                refreshed["enqueued_at"] = now
                await self.redis.lpush(self.queue_key, json.dumps(refreshed) if refreshed else text)
                moved += 1
        return moved

    # Per-monitor in-flight lock so two workers never run the same monitor concurrently.
    async def acquire_inflight(self, monitor_id: str, ttl_seconds: int) -> bool:
        return bool(
            await self.redis.set(f"{INFLIGHT_PREFIX}{monitor_id}", "1", nx=True, ex=ttl_seconds)
        )

    async def release_inflight(self, monitor_id: str) -> None:
        await self.redis.delete(f"{INFLIGHT_PREFIX}{monitor_id}")


async def beat(redis: Redis, name: str, ttl_seconds: int) -> None:
    await redis.set(f"{HEARTBEAT_PREFIX}{name}", str(time.time()), ex=ttl_seconds)


async def is_alive(redis: Redis, name: str) -> bool:
    return bool(await redis.exists(f"{HEARTBEAT_PREFIX}{name}"))
