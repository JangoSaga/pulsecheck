"""Container healthcheck for worker processes: ``python -m app.healthcheck <name>``.

Workers write a short-lived heartbeat key to Redis; healthy means the key exists.
"""

import asyncio
import sys

from redis.asyncio import Redis

from app.config import get_settings
from app.services.queue import is_alive


async def _main(name: str) -> int:
    redis = Redis.from_url(get_settings().redis_url)
    try:
        return 0 if await is_alive(redis, name) else 1
    except Exception:
        return 1
    finally:
        await redis.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1])))
