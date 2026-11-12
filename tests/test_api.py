import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Incident, Monitor, MonitorCheck, User
from app.security import create_access_token
from tests.conftest import VALID_MONITOR, register

BASE = "/api/v1/monitors"


async def create(client, auth, **over):
    r = await client.post(BASE, json={**VALID_MONITOR, **over}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- auth


async def test_register_login_me(client):
    r = await client.post(
        "/api/v1/auth/register", json={"email": "New@Example.com", "password": "longenough1"}
    )
    assert r.status_code == 201
    assert r.json()["email"] == "new@example.com"
    assert "password" not in r.text
    headers = await register(client, "other@example.com")
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.json()["email"] == "other@example.com"


async def test_password_is_stored_hashed(client, sessionmaker):
    await register(client, "h@example.com", "plaintext-pass")
    async with sessionmaker() as s:
        user = (await s.execute(select(User))).scalar_one()
    assert "plaintext-pass" not in user.password_hash
    assert user.password_hash.startswith("$2")


async def test_duplicate_registration_conflicts(client):
    await register(client, "dup@example.com")
    r = await client.post(
        "/api/v1/auth/register", json={"email": "DUP@example.com", "password": "longenough1"}
    )
    assert r.status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "not-an-email", "password": "longenough1"},
        {"email": "a@example.com", "password": "short"},
        {"email": "a@example.com", "password": "x" * 80},
        {"email": "a@example.com"},
    ],
)
async def test_invalid_registration(client, payload):
    assert (await client.post("/api/v1/auth/register", json=payload)).status_code == 422


async def test_login_failures_are_uniform(client):
    await register(client, "a@example.com")
    wrong = await client.post(
        "/api/v1/auth/login", json={"email": "a@example.com", "password": "wrong-password"}
    )
    unknown = await client.post(
        "/api/v1/auth/login", json={"email": "zzz@example.com", "password": "wrong-password"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


async def test_endpoints_require_authentication(client):
    mid = uuid.uuid4()
    calls = [
        ("POST", BASE),
        ("GET", BASE),
        ("GET", f"{BASE}/{mid}"),
        ("PATCH", f"{BASE}/{mid}"),
        ("DELETE", f"{BASE}/{mid}"),
        ("POST", f"{BASE}/{mid}/pause"),
        ("POST", f"{BASE}/{mid}/resume"),
        ("GET", f"{BASE}/{mid}/checks"),
        ("GET", f"{BASE}/{mid}/incidents"),
        ("GET", "/api/v1/analytics/uptime"),
    ]
    for method, url in calls:
        r = await client.request(method, url, json={} if method in ("POST", "PATCH") else None)
        assert r.status_code == 401, (method, url)


async def test_bad_and_expired_tokens_rejected(client, settings):
    r = await client.get(BASE, headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401
    expired = settings.model_copy(update={"jwt_expire_minutes": -5})
    token = create_access_token(uuid.uuid4(), expired)
    r = await client.get(BASE, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


async def test_token_for_deleted_user_rejected(client, settings, sessionmaker):
    headers = await register(client, "gone@example.com")
    async with sessionmaker() as s:
        await s.delete((await s.execute(select(User))).scalar_one())
        await s.commit()
    assert (await client.get(BASE, headers=headers)).status_code == 401


# ------------------------------------------------------- monitor CRUD


async def test_create_and_get_monitor(client, auth):
    m = await create(client, auth, expected_status_codes=[200, 204, 200])
    assert m["expected_status_codes"] == [200, 204]
    assert m["http_method"] == "GET" and m["is_active"] is True
    assert m["failure_threshold"] == 3 and m["recovery_threshold"] == 2
    assert m["status"] == "unknown"
    got = await client.get(f"{BASE}/{m['id']}", headers=auth)
    assert got.status_code == 200 and got.json()["id"] == m["id"]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://example.com:8080/path?x=1",
        "https://sub.example.org/health",
    ],
)
async def test_valid_monitor_urls(client, auth, url):
    r = await client.post(BASE, json={**VALID_MONITOR, "target_url": url}, headers=auth)
    assert r.status_code == 201


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com",
        "file:///etc/passwd",
        "not a url",
        "example.com",
        "http://user:pw@example.com",
        "http://127.0.0.1",
        "http://localhost:8000/admin",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://metadata.google.internal/",
        "http://internal.example/",
        "http://loopback.example/",
    ],
)
async def test_invalid_or_unsafe_monitor_urls_rejected(client, auth, url):
    r = await client.post(BASE, json={**VALID_MONITOR, "target_url": url}, headers=auth)
    assert r.status_code == 422, (url, r.text)


@pytest.mark.parametrize(
    "override",
    [
        {"interval_seconds": 5},  # below MIN_INTERVAL_SECONDS (10)
        {"interval_seconds": 0},
        {"interval_seconds": 100000},
        {"timeout_seconds": 0},
        {"timeout_seconds": 61},
        {"interval_seconds": 20, "timeout_seconds": 30},
        {"expected_status_codes": []},
        {"expected_status_codes": [99]},
        {"expected_status_codes": [600]},
        {"expected_status_codes": ["ok"]},
        {"http_method": "DELETE"},
        {"failure_threshold": 0},
        {"recovery_threshold": 0},
        {"name": ""},
        {"name": "bad\nname"},
        {"name": "x" * 121},
    ],
)
async def test_invalid_monitor_fields(client, auth, override):
    r = await client.post(BASE, json={**VALID_MONITOR, **override}, headers=auth)
    assert r.status_code == 422, (override, r.text)


async def test_update_monitor(client, auth):
    m = await create(client, auth)
    r = await client.patch(
        f"{BASE}/{m['id']}",
        json={"name": "Renamed", "interval_seconds": 30, "expected_status_codes": [200, 301]},
        headers=auth,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Renamed" and body["interval_seconds"] == 30
    assert body["expected_status_codes"] == [200, 301]


async def test_update_revalidates_url_and_limits(client, auth):
    m = await create(client, auth)
    for patch in (
        {"target_url": "http://127.0.0.1/"},
        {"target_url": "http://10.0.0.1/"},
        {"timeout_seconds": 59},  # exceeds the interval (60)? no, but interval below makes it so
        {"interval_seconds": 1},
        {"name": None},
        {"unknown_field": 1},
    ):
        if patch == {"timeout_seconds": 59}:
            patch = {"timeout_seconds": 59, "interval_seconds": 20}
        r = await client.patch(f"{BASE}/{m['id']}", json=patch, headers=auth)
        assert r.status_code == 422, (patch, r.text)


async def test_pause_resume_delete(client, auth):
    m = await create(client, auth)
    url = f"{BASE}/{m['id']}"
    paused = await client.post(f"{url}/pause", headers=auth)
    assert paused.json()["is_active"] is False
    resumed = await client.post(f"{url}/resume", headers=auth)
    assert resumed.json()["is_active"] is True
    assert (await client.delete(url, headers=auth)).status_code == 204
    assert (await client.get(url, headers=auth)).status_code == 404
    assert (await client.delete(url, headers=auth)).status_code == 404


async def test_resume_resets_streaks_and_schedules_immediately(client, auth, sessionmaker):
    m = await create(client, auth)
    async with sessionmaker() as s:
        row = await s.get(Monitor, uuid.UUID(m["id"]))
        row.consecutive_failures = 5
        row.next_check_at = datetime.now(UTC) + timedelta(days=1)
        await s.commit()
    await client.post(f"{BASE}/{m['id']}/pause", headers=auth)
    await client.post(f"{BASE}/{m['id']}/resume", headers=auth)
    async with sessionmaker() as s:
        row = await s.get(Monitor, uuid.UUID(m["id"]))
    assert row.consecutive_failures == 0
    assert row.next_check_at <= datetime.now(UTC)


async def test_delete_cascades_history(client, auth, sessionmaker):
    m = await create(client, auth)
    mid = uuid.UUID(m["id"])
    async with sessionmaker() as s:
        s.add(MonitorCheck(monitor_id=mid, outcome="healthy", status_code=200))
        s.add(Incident(monitor_id=mid, status="open", failure_reason="x"))
        await s.commit()
    await client.delete(f"{BASE}/{m['id']}", headers=auth)
    async with sessionmaker() as s:
        assert (await s.execute(select(MonitorCheck))).first() is None
        assert (await s.execute(select(Incident))).first() is None


async def test_monitor_limit_per_user(client, auth, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "max_monitors_per_user", 2)
    await create(client, auth)
    await create(client, auth)
    r = await client.post(BASE, json=VALID_MONITOR, headers=auth)
    assert r.status_code == 409


# --------------------------------------------------------- ownership


async def test_user_cannot_touch_another_users_monitor(client, auth, auth_b):
    m = await create(client, auth)
    url = f"{BASE}/{m['id']}"
    attempts = [
        ("GET", url, None),
        ("PATCH", url, {"name": "hijack"}),
        ("DELETE", url, None),
        ("POST", f"{url}/pause", None),
        ("POST", f"{url}/resume", None),
        ("GET", f"{url}/checks", None),
        ("GET", f"{url}/incidents", None),
        ("GET", f"/api/v1/analytics/uptime?monitor_id={m['id']}", None),
    ]
    for method, u, body in attempts:
        r = await client.request(method, u, json=body, headers=auth_b)
        assert r.status_code == 404, (method, u, r.status_code)
    # Still intact and unchanged for the owner.
    mine = await client.get(url, headers=auth)
    assert mine.status_code == 200 and mine.json()["name"] == "Example"
    assert mine.json()["is_active"] is True


async def test_list_only_returns_own_monitors(client, auth, auth_b):
    await create(client, auth, name="a1")
    await create(client, auth, name="a2")
    await create(client, auth_b, name="b1")
    mine = (await client.get(BASE, headers=auth)).json()
    theirs = (await client.get(BASE, headers=auth_b)).json()
    assert mine["total"] == 2 and {i["name"] for i in mine["items"]} == {"a1", "a2"}
    assert theirs["total"] == 1 and theirs["items"][0]["name"] == "b1"


async def test_unknown_monitor_id_is_404_and_bad_uuid_is_422(client, auth):
    assert (await client.get(f"{BASE}/{uuid.uuid4()}", headers=auth)).status_code == 404
    assert (await client.get(f"{BASE}/not-a-uuid", headers=auth)).status_code == 422


# ------------------------------------------------ pagination, history


async def test_monitor_list_pagination(client, auth):
    for i in range(5):
        await create(client, auth, name=f"m{i}")
    p1 = (await client.get(f"{BASE}?limit=2&offset=0", headers=auth)).json()
    p2 = (await client.get(f"{BASE}?limit=2&offset=2", headers=auth)).json()
    p3 = (await client.get(f"{BASE}?limit=2&offset=4", headers=auth)).json()
    assert p1["total"] == 5 and len(p1["items"]) == 2 and p1["limit"] == 2
    ids = [i["id"] for p in (p1, p2, p3) for i in p["items"]]
    assert len(ids) == 5 and len(set(ids)) == 5
    assert (await client.get(f"{BASE}?offset=99", headers=auth)).json()["items"] == []


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "offset=-1", "limit=abc"])
async def test_invalid_pagination_rejected(client, auth, query):
    m = await create(client, auth)
    for url in (BASE, f"{BASE}/{m['id']}/checks", f"{BASE}/{m['id']}/incidents"):
        assert (await client.get(f"{url}?{query}", headers=auth)).status_code == 422


async def test_checks_history_filters_and_orders(client, auth, sessionmaker):
    m = await create(client, auth)
    mid = uuid.UUID(m["id"])
    now = datetime.now(UTC)
    async with sessionmaker() as s:
        for i in range(6):
            s.add(
                MonitorCheck(
                    monitor_id=mid,
                    outcome="healthy" if i % 2 == 0 else "unhealthy",
                    status_code=200 if i % 2 == 0 else 500,
                    response_time_ms=10 * i,
                    checked_at=now - timedelta(minutes=i),
                )
            )
        s.add(MonitorCheck(monitor_id=mid, outcome="healthy", checked_at=now - timedelta(days=3)))
        await s.commit()
    r = (await client.get(f"{BASE}/{m['id']}/checks", headers=auth)).json()
    assert r["total"] == 6  # default window is the last 24h
    times = [c["checked_at"] for c in r["items"]]
    assert times == sorted(times, reverse=True)
    bad = (await client.get(f"{BASE}/{m['id']}/checks?outcome=unhealthy", headers=auth)).json()
    assert bad["total"] == 3 and all(c["outcome"] == "unhealthy" for c in bad["items"])
    wide = (
        await client.get(
            f"{BASE}/{m['id']}/checks",
            params={
                "from": (now - timedelta(days=5)).isoformat(),
                "to": (now + timedelta(minutes=1)).isoformat(),
            },
            headers=auth,
        )
    ).json()
    assert wide["total"] == 7
    page = (await client.get(f"{BASE}/{m['id']}/checks?limit=4&offset=4", headers=auth)).json()
    assert len(page["items"]) == 2 and page["total"] == 6


async def test_invalid_date_ranges_rejected(client, auth):
    m = await create(client, auth)
    now = datetime.now(UTC)
    urls = [
        f"{BASE}/{m['id']}/checks",
        f"{BASE}/{m['id']}/incidents",
        "/api/v1/analytics/uptime",
    ]
    cases = [
        {"from": now.isoformat(), "to": (now - timedelta(hours=1)).isoformat()},
        {"from": now.isoformat(), "to": now.isoformat()},
        {"from": (now - timedelta(days=91)).isoformat(), "to": now.isoformat()},
        {"from": "yesterday"},
        {"to": "not-a-date"},
    ]
    for url in urls:
        for params in cases:
            r = await client.get(url, params=params, headers=auth)
            assert r.status_code == 422, (url, params)
    ok = await client.get(
        urls[0],
        params={"from": (now - timedelta(days=90)).isoformat(), "to": now.isoformat()},
        headers=auth,
    )
    assert ok.status_code == 200


async def test_incident_history_with_notifications(client, auth, sessionmaker):
    m = await create(client, auth)
    from app.services.state import record_result
    from tests.conftest import result

    await client.patch(f"{BASE}/{m['id']}", json={"failure_threshold": 1}, headers=auth)
    async with sessionmaker() as s:
        await record_result(s, uuid.UUID(m["id"]), result(False))
    r = (await client.get(f"{BASE}/{m['id']}/incidents", headers=auth)).json()
    assert r["total"] == 1
    inc = r["items"][0]
    assert inc["status"] == "open" and inc["resolved_at"] is None
    assert inc["notifications"][0]["event"] == "opened"
    only_open = await client.get(f"{BASE}/{m['id']}/incidents?status=resolved", headers=auth)
    assert only_open.json()["total"] == 0
