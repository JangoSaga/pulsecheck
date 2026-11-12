"""Test infrastructure.

Integration tests use the Postgres and Redis from docker-compose (ports 5433 and 6380)
or any instances named by TEST_DATABASE_URL and TEST_REDIS_URL. A dedicated database
(pulsecheck_test) and Redis db 15 are used, never the application's data.
"""

import os
import threading
import time
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://pulsecheck:pulsecheck@localhost:5433/pulsecheck_test",
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6380/15")

os.environ["DATABASE_URL"] = TEST_DB_URL
os.environ["REDIS_URL"] = TEST_REDIS_URL
os.environ["ENVIRONMENT"] = "test"
os.environ["ALLOW_PRIVATE_TARGETS"] = "false"
os.environ["MIN_INTERVAL_SECONDS"] = "10"
os.environ["BCRYPT_ROUNDS"] = "4"

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from redis.asyncio import Redis  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import get_session  # noqa: E402
from app.deps import get_resolver  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, Monitor, User  # noqa: E402
from app.services.checker import UNHEALTHY, CheckResult  # noqa: E402

PUBLIC_IP = "93.184.216.34"
FAKE_DNS = {
    "internal.example": ["10.0.0.5"],
    "loopback.example": ["127.0.0.1"],
    "metadata.example": ["169.254.169.254"],
    "mixed.example": [PUBLIC_IP, "192.168.1.10"],
    "nxdomain.example": [],
}


async def fake_resolver(host: str, port: int) -> list[str]:
    return FAKE_DNS.get(host, [PUBLIC_IP])


@pytest_asyncio.fixture(scope="session")
async def engine():
    url = make_url(TEST_DB_URL)
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        exists = await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database}
        )
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    await admin.dispose()
    eng = create_async_engine(TEST_DB_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture(scope="session")
def sessionmaker(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def clean(engine):
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE users CASCADE"))
    redis = Redis.from_url(TEST_REDIS_URL)
    await redis.flushdb()
    await redis.aclose()
    yield


@pytest_asyncio.fixture
async def redis():
    r = Redis.from_url(TEST_REDIS_URL)
    yield r
    await r.aclose()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url=TEST_DB_URL,
        redis_url=TEST_REDIS_URL,
        allow_private_targets=True,
        notify_retry_base_seconds=30,
        notify_max_attempts=3,
    )


@pytest_asyncio.fixture
async def client(sessionmaker):
    async def _session():
        async with sessionmaker() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_resolver] = lambda: fake_resolver
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def register(client: httpx.AsyncClient, email: str, password: str = "correct-horse-1"):
    r = await client.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest_asyncio.fixture
async def auth(client):
    return await register(client, "alice@example.com")


@pytest_asyncio.fixture
async def auth_b(client):
    return await register(client, "bob@example.com")


VALID_MONITOR = {
    "name": "Example",
    "target_url": "https://example.com/health",
    "interval_seconds": 60,
    "timeout_seconds": 10,
}


async def make_user(sessionmaker, email: str = "owner@example.com") -> User:
    async with sessionmaker() as s:
        user = User(email=email, password_hash="x")
        s.add(user)
        await s.commit()
        return user


async def make_monitor(sessionmaker, user_id: uuid.UUID, **overrides) -> Monitor:
    fields = {
        "name": "m",
        "target_url": "https://example.com",
        "http_method": "GET",
        "interval_seconds": 60,
        "timeout_seconds": 5,
        "expected_status_codes": [200],
        "failure_threshold": 3,
        "recovery_threshold": 2,
        "is_active": True,
        "next_check_at": datetime.now(UTC),
    }
    fields.update(overrides)
    async with sessionmaker() as s:
        monitor = Monitor(user_id=user_id, **fields)
        s.add(monitor)
        await s.commit()
        return monitor


def result(healthy: bool, status: int | None = None, when: datetime | None = None) -> CheckResult:
    when = when or datetime.now(UTC)
    if healthy:
        return CheckResult("healthy", status or 200, 42, None, None, when)
    return CheckResult(UNHEALTHY, status or 500, 42, "unexpected_status", "bad", when)


class LocalServer:
    def __init__(self) -> None:
        self.hits: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.hits.append(self.path)
                if self.path.startswith("/status/"):
                    self._reply(int(self.path.rsplit("/", 1)[1]))
                elif self.path.startswith("/delay/"):
                    time.sleep(float(self.path.rsplit("/", 1)[1]))
                    self._reply(200)
                elif self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self._reply(404)

            do_HEAD = do_GET
            do_POST = do_GET

            def _reply(self, code: int) -> None:
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def local_server():
    server = LocalServer()
    yield server
    server.close()
