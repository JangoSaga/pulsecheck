import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text

from app.api import analytics, auth, monitors
from app.config import get_settings
from app.db import get_engine
from app.logging import configure_logging, new_correlation_id, set_correlation_id

log = logging.getLogger("app.request")

CORRELATION_HEADER = "X-Request-ID"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(get_settings().log_level)
    yield
    await get_engine().dispose()


app = FastAPI(title="PulseCheck", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def correlation_middleware(request: Request, call_next) -> Response:
    incoming = request.headers.get(CORRELATION_HEADER, "")
    # Accept a caller supplied id only if it is a sane token; otherwise generate one.
    cid = (
        incoming if 0 < len(incoming) <= 64 and incoming.isascii() and incoming.isalnum() else None
    )
    cid = cid or new_correlation_id()
    set_correlation_id(cid)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled error", extra={"path": request.url.path})
        response = JSONResponse({"detail": "Internal server error"}, status_code=500)
    response.headers[CORRELATION_HEADER] = cid
    log.info(
        "request",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        },
    )
    set_correlation_id(None)
    return response


app.include_router(auth.router)
app.include_router(monitors.router)
app.include_router(analytics.router)


@app.get("/healthz", tags=["health"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz", tags=["health"])
async def readyz() -> JSONResponse:
    settings = get_settings()
    checks: dict[str, str] = {}
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "error"
    redis = Redis.from_url(settings.redis_url)
    try:
        await redis.ping()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "error"
    finally:
        await redis.aclose()
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ok" if ok else "unavailable", **checks}, status_code=200 if ok else 503
    )
