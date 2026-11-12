import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models import Incident, Monitor, MonitorCheck
from tests.conftest import VALID_MONITOR

URL = "/api/v1/analytics/uptime"


async def seed(sessionmaker, client, auth, *, interval=60, created_ago=timedelta(hours=2)):
    r = await client.post(
        "/api/v1/monitors",
        json={**VALID_MONITOR, "interval_seconds": interval},
        headers=auth,
    )
    mid = uuid.UUID(r.json()["id"])
    async with sessionmaker() as s:
        m = await s.get(Monitor, mid)
        m.created_at = datetime.now(UTC) - created_ago
        await s.commit()
    return mid


async def add_checks(sessionmaker, mid, rows, now):
    async with sessionmaker() as s:
        for healthy, rt, minutes_ago in rows:
            s.add(
                MonitorCheck(
                    monitor_id=mid,
                    outcome="healthy" if healthy else "unhealthy",
                    status_code=200 if healthy else 500,
                    response_time_ms=rt,
                    checked_at=now - timedelta(minutes=minutes_ago),
                )
            )
        await s.commit()


async def test_uptime_counts_and_percentage(client, auth, sessionmaker):
    mid = await seed(sessionmaker, client, auth)
    now = datetime.now(UTC)
    rows = [(True, 100, i + 1) for i in range(8)] + [(False, 50, 20), (False, None, 21)]
    await add_checks(sessionmaker, mid, rows, now)
    r = await client.get(URL, params={"monitor_id": str(mid)}, headers=auth)
    assert r.status_code == 200
    s = r.json()["monitors"][0]
    assert s["total_checks"] == 10
    assert s["successful_checks"] == 8 and s["failed_checks"] == 2
    assert s["uptime_percentage"] == 80.0


async def test_response_time_average_and_percentiles(client, auth, sessionmaker):
    mid = await seed(sessionmaker, client, auth)
    now = datetime.now(UTC)
    rows = [(True, 10 * (i + 1), i + 1) for i in range(10)]  # 10..100 ms
    await add_checks(sessionmaker, mid, rows, now)
    s = (await client.get(URL, params={"monitor_id": str(mid)}, headers=auth)).json()["monitors"][0]
    assert s["avg_response_time_ms"] == 55.0
    assert s["p50_response_time_ms"] == 55.0  # continuous percentile, linear interpolation
    assert s["p95_response_time_ms"] == 95.5
    assert s["p99_response_time_ms"] == 99.1


async def test_checks_without_response_time_are_excluded_from_latency_only(
    client, auth, sessionmaker
):
    mid = await seed(sessionmaker, client, auth)
    now = datetime.now(UTC)
    await add_checks(sessionmaker, mid, [(True, 100, 1), (False, None, 2)], now)
    s = (await client.get(URL, params={"monitor_id": str(mid)}, headers=auth)).json()["monitors"][0]
    assert s["total_checks"] == 2 and s["uptime_percentage"] == 50.0
    assert s["avg_response_time_ms"] == 100.0


async def test_no_checks_gives_null_not_zero_or_hundred(client, auth, sessionmaker):
    mid = await seed(sessionmaker, client, auth)
    s = (await client.get(URL, params={"monitor_id": str(mid)}, headers=auth)).json()["monitors"][0]
    assert s["total_checks"] == 0
    assert s["uptime_percentage"] is None
    assert s["avg_response_time_ms"] is None and s["p95_response_time_ms"] is None
    assert s["incident_count"] == 0 and s["total_incident_seconds"] == 0


async def test_missing_checks_are_not_counted_as_downtime_but_show_in_coverage(
    client, auth, sessionmaker
):
    mid = await seed(sessionmaker, client, auth, interval=60, created_ago=timedelta(hours=3))
    now = datetime.now(UTC)
    await add_checks(sessionmaker, mid, [(True, 10, i + 1) for i in range(15)], now)
    end = now
    start = now - timedelta(hours=1)
    s = (
        await client.get(
            URL,
            params={"monitor_id": str(mid), "from": start.isoformat(), "to": end.isoformat()},
            headers=auth,
        )
    ).json()["monitors"][0]
    assert s["uptime_percentage"] == 100.0  # gaps do not reduce uptime
    assert s["expected_checks"] == 60
    assert s["coverage_percentage"] == 25.0  # but they are visible


async def test_expected_checks_clipped_to_monitor_lifetime(client, auth, sessionmaker):
    mid = await seed(sessionmaker, client, auth, interval=60, created_ago=timedelta(minutes=10))
    now = datetime.now(UTC)
    s = (
        await client.get(
            URL,
            params={
                "monitor_id": str(mid),
                "from": (now - timedelta(hours=1)).isoformat(),
                "to": now.isoformat(),
            },
            headers=auth,
        )
    ).json()["monitors"][0]
    assert 9 <= s["expected_checks"] <= 10


async def test_window_filters_checks(client, auth, sessionmaker):
    mid = await seed(sessionmaker, client, auth)
    now = datetime.now(UTC)
    await add_checks(sessionmaker, mid, [(True, 10, 5), (False, 10, 100)], now)
    s = (
        await client.get(
            URL,
            params={
                "monitor_id": str(mid),
                "from": (now - timedelta(minutes=30)).isoformat(),
                "to": now.isoformat(),
            },
            headers=auth,
        )
    ).json()["monitors"][0]
    assert s["total_checks"] == 1 and s["uptime_percentage"] == 100.0


async def test_incident_count_and_duration_clipped_to_window(client, auth, sessionmaker):
    mid = await seed(sessionmaker, client, auth)
    now = datetime.now(UTC)
    async with sessionmaker() as s:
        s.add_all(
            [
                # Started before the window, resolved inside it: 30 minutes counted.
                Incident(
                    monitor_id=mid,
                    status="resolved",
                    failure_reason="a",
                    started_at=now - timedelta(hours=2),
                    resolved_at=now - timedelta(minutes=30),
                ),
                # Fully inside the window: 10 minutes.
                Incident(
                    monitor_id=mid,
                    status="resolved",
                    failure_reason="b",
                    started_at=now - timedelta(minutes=20),
                    resolved_at=now - timedelta(minutes=10),
                ),
                # Entirely before the window: ignored.
                Incident(
                    monitor_id=mid,
                    status="resolved",
                    failure_reason="c",
                    started_at=now - timedelta(hours=5),
                    resolved_at=now - timedelta(hours=4),
                ),
                # Still open, started 5 minutes ago: counted up to now.
                Incident(
                    monitor_id=mid,
                    status="open",
                    failure_reason="d",
                    started_at=now - timedelta(minutes=5),
                ),
            ]
        )
        await s.commit()
    s = (
        await client.get(
            URL,
            params={
                "monitor_id": str(mid),
                "from": (now - timedelta(hours=1)).isoformat(),
                "to": now.isoformat(),
            },
            headers=auth,
        )
    ).json()["monitors"][0]
    assert s["incident_count"] == 3
    expected = 30 * 60 + 10 * 60 + 5 * 60
    assert expected - 5 <= s["total_incident_seconds"] <= expected + 5


async def test_all_monitors_by_default_and_status_reporting(client, auth, auth_b, sessionmaker):
    a = await seed(sessionmaker, client, auth)
    await seed(sessionmaker, client, auth)
    await seed(sessionmaker, client, auth_b)
    await client.post(f"/api/v1/monitors/{a}/pause", headers=auth)
    body = (await client.get(URL, headers=auth)).json()
    assert len(body["monitors"]) == 2  # never includes other users' monitors
    statuses = {m["monitor_id"]: m["current_status"] for m in body["monitors"]}
    assert statuses[str(a)] == "paused"
    assert "unknown" in statuses.values()


@pytest.mark.parametrize("path", ["/api/v1/analytics/uptime?monitor_id=nope"])
async def test_bad_monitor_id(client, auth, path):
    assert (await client.get(path, headers=auth)).status_code == 422
