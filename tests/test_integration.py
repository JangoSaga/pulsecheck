"""End-to-end pipeline against a controlled local HTTP server.

API -> scheduler claim -> Redis queue -> check worker -> state machine -> notification
worker (fake SMTP sender). Only DNS (not needed for IP literals) and SMTP are stubbed.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.config import Settings
from app.models import Monitor, MonitorCheck, Notification
from app.services.notifier import deliver_due_notifications
from app.services.queue import CheckQueue
from app.services.scheduling import claim_due_monitors
from app.workers.check_worker import process_job
from tests.conftest import VALID_MONITOR


class Mailbox:
    def __init__(self):
        self.mails = []

    async def __call__(self, mail):
        self.mails.append(mail)


async def run_cycle(sessionmaker, redis, settings, now):
    """One scheduler tick followed by workers draining the queue."""
    q = CheckQueue(redis)
    await claim_due_monitors(sessionmaker, q.enqueue, now)
    outcomes = []
    while (raw := await q.take(block_seconds=1)) is not None:
        outcomes.append(
            await process_job(raw, sessionmaker=sessionmaker, queue=q, settings=settings)
        )
    return outcomes


async def test_full_outage_and_recovery_flow(client, auth, sessionmaker, redis, local_server):
    settings = Settings(_env_file=None, allow_private_targets=True)
    # The API refuses private targets by default; creating the monitor through the API
    # with the dev override enabled mirrors the compose dev setup.
    from app.config import get_settings

    api_settings = get_settings()
    api_settings.allow_private_targets = True
    try:
        r = await client.post(
            "/api/v1/monitors",
            json={
                **VALID_MONITOR,
                "target_url": local_server.url("/status/200"),
                "failure_threshold": 2,
                "recovery_threshold": 2,
            },
            headers=auth,
        )
        assert r.status_code == 201, r.text
        mid = r.json()["id"]
        now = datetime.now(UTC)
        mailbox = Mailbox()

        async def send_mail(t):
            return await deliver_due_notifications(
                sessionmaker, mailbox, t, max_attempts=3, retry_base_seconds=30
            )

        # Healthy checks are recorded.
        assert await run_cycle(sessionmaker, redis, settings, now) == ["stored"]
        checks = (await client.get(f"/api/v1/monitors/{mid}/checks", headers=auth)).json()
        assert checks["total"] == 1 and checks["items"][0]["outcome"] == "healthy"
        assert (await client.get(f"/api/v1/monitors/{mid}", headers=auth)).json()["status"] == "up"

        # Break the target. One failure is not an incident yet.
        await client.patch(
            f"/api/v1/monitors/{mid}",
            json={"target_url": local_server.url("/status/500")},
            headers=auth,
        )
        now += timedelta(seconds=61)
        await run_cycle(sessionmaker, redis, settings, now)
        inc = (await client.get(f"/api/v1/monitors/{mid}/incidents", headers=auth)).json()
        assert inc["total"] == 0

        # Second consecutive failure opens exactly one incident and one notification.
        now += timedelta(seconds=61)
        await run_cycle(sessionmaker, redis, settings, now)
        now += timedelta(seconds=61)
        await run_cycle(sessionmaker, redis, settings, now)
        inc = (await client.get(f"/api/v1/monitors/{mid}/incidents", headers=auth)).json()
        assert inc["total"] == 1 and inc["items"][0]["status"] == "open"
        assert "500" in inc["items"][0]["failure_reason"]
        assert await send_mail(now) == 1
        assert await send_mail(now) == 0
        assert len(mailbox.mails) == 1 and "DOWN" in mailbox.mails[0].subject
        assert mailbox.mails[0].to == "alice@example.com"

        # Recover: two healthy checks resolve it and send one recovery mail.
        await client.patch(
            f"/api/v1/monitors/{mid}",
            json={"target_url": local_server.url("/status/200")},
            headers=auth,
        )
        for _ in range(2):
            now += timedelta(seconds=61)
            await run_cycle(sessionmaker, redis, settings, now)
        inc = (await client.get(f"/api/v1/monitors/{mid}/incidents", headers=auth)).json()
        assert inc["items"][0]["status"] == "resolved" and inc["items"][0]["resolved_at"]
        await send_mail(now)
        assert [("DOWN" in m.subject) for m in mailbox.mails] == [True, False]
        assert "RECOVERED" in mailbox.mails[1].subject
        inc = (await client.get(f"/api/v1/monitors/{mid}/incidents", headers=auth)).json()
        notes = inc["items"][0]["notifications"]
        assert sorted((n["event"], n["status"]) for n in notes) == [
            ("opened", "sent"),
            ("resolved", "sent"),
        ]

        # Analytics reflect the stored history.
        up = (await client.get("/api/v1/analytics/uptime", headers=auth)).json()["monitors"][0]
        assert up["total_checks"] >= 6
        assert up["failed_checks"] >= 2 and up["incident_count"] == 1
        assert 0 < up["uptime_percentage"] < 100
    finally:
        api_settings.allow_private_targets = False


async def test_paused_monitor_gets_no_checks_and_deleted_stops(
    client, auth, sessionmaker, redis, local_server
):
    from app.config import get_settings

    api_settings = get_settings()
    api_settings.allow_private_targets = True
    settings = Settings(_env_file=None, allow_private_targets=True)
    try:
        r = await client.post(
            "/api/v1/monitors",
            json={**VALID_MONITOR, "target_url": local_server.url("/status/200")},
            headers=auth,
        )
        mid = r.json()["id"]
        now = datetime.now(UTC)
        await client.post(f"/api/v1/monitors/{mid}/pause", headers=auth)
        assert await run_cycle(sessionmaker, redis, settings, now) == []
        assert local_server.hits == []
        await client.post(f"/api/v1/monitors/{mid}/resume", headers=auth)
        now = datetime.now(UTC) + timedelta(seconds=1)
        assert await run_cycle(sessionmaker, redis, settings, now) == ["stored"]
        await client.delete(f"/api/v1/monitors/{mid}", headers=auth)
        assert await run_cycle(sessionmaker, redis, settings, now + timedelta(hours=1)) == []
        async with sessionmaker() as s:
            assert (await s.execute(select(Monitor))).first() is None
            assert (await s.execute(select(MonitorCheck))).first() is None
    finally:
        api_settings.allow_private_targets = False


async def test_in_flight_check_result_discarded_after_pause(
    sessionmaker, redis, local_server, auth, client
):
    """A user pauses while a check is running: the late result must not create state."""
    from app.config import get_settings
    from app.services.checker import perform_check
    from app.services.state import record_result

    api_settings = get_settings()
    api_settings.allow_private_targets = True
    try:
        r = await client.post(
            "/api/v1/monitors",
            json={
                **VALID_MONITOR,
                "target_url": local_server.url("/status/500"),
                "failure_threshold": 1,
            },
            headers=auth,
        )
        mid = r.json()["id"]
        import uuid

        res = await perform_check(
            local_server.url("/status/500"), "GET", 5, [200], allow_private=True
        )
        await client.post(f"/api/v1/monitors/{mid}/pause", headers=auth)
        async with sessionmaker() as s:
            assert await record_result(s, uuid.UUID(mid), res) is None
        async with sessionmaker() as s:
            assert (await s.execute(select(Notification))).first() is None
    finally:
        api_settings.allow_private_targets = False


async def test_private_target_is_recorded_as_blocked_when_guard_is_on(
    sessionmaker, redis, local_server
):
    from tests.conftest import make_monitor, make_user

    user = await make_user(sessionmaker)
    await make_monitor(sessionmaker, user.id, target_url=local_server.url("/status/200"))
    strict = Settings(_env_file=None, allow_private_targets=False)
    assert await run_cycle(sessionmaker, redis, strict, datetime.now(UTC)) == ["stored"]
    async with sessionmaker() as s:
        check = (await s.execute(select(MonitorCheck))).scalar_one()
    assert check.outcome == "unhealthy" and check.error_type == "ssrf_blocked"
    assert local_server.hits == []
