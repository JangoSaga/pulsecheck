import asyncio
from datetime import UTC, datetime, timedelta

import aiosmtplib
import pytest
from sqlalchemy import func, select

from app.models import Incident, Notification
from app.services import notifier
from app.services.notifier import (
    Email,
    PermanentDeliveryError,
    TemporaryDeliveryError,
    deliver_due_notifications,
    smtp_sender,
)
from app.services.state import _enqueue_notification, record_result
from tests.conftest import make_monitor, make_user, result


class FakeSender:
    def __init__(self, failures=None):
        self.sent: list[Email] = []
        self.failures = list(failures or [])
        self.delay = 0.0

    async def __call__(self, mail: Email):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failures:
            exc = self.failures.pop(0)
            if exc is not None:
                raise exc
        self.sent.append(mail)


async def open_incident(sessionmaker, email="owner@example.com"):
    user = await make_user(sessionmaker, email)
    m = await make_monitor(sessionmaker, user.id, name="API prod", failure_threshold=1)
    async with sessionmaker() as s:
        await record_result(s, m.id, result(False))
    return m


async def deliver(sessionmaker, sender, now, **kw):
    return await deliver_due_notifications(
        sessionmaker,
        sender,
        now,
        max_attempts=kw.get("max_attempts", 3),
        retry_base_seconds=kw.get("base", 30),
    )


async def only_notification(sessionmaker) -> Notification:
    async with sessionmaker() as s:
        return (await s.execute(select(Notification))).scalar_one()


async def test_opened_notification_is_sent_and_recorded(sessionmaker):
    await open_incident(sessionmaker)
    sender = FakeSender()
    now = datetime.now(UTC)
    assert await deliver(sessionmaker, sender, now) == 1
    n = await only_notification(sessionmaker)
    assert n.status == "sent" and n.attempt_count == 1 and n.sent_at is not None
    assert n.last_error is None
    mail = sender.sent[0]
    assert mail.to == "owner@example.com"
    assert "DOWN" in mail.subject and "API prod" in mail.subject


async def test_sent_notification_is_not_resent(sessionmaker):
    await open_incident(sessionmaker)
    sender = FakeSender()
    now = datetime.now(UTC)
    await deliver(sessionmaker, sender, now)
    assert await deliver(sessionmaker, sender, now + timedelta(hours=1)) == 0
    assert len(sender.sent) == 1


async def test_resolved_notification_follows(sessionmaker):
    m = await open_incident(sessionmaker)
    sender = FakeSender()
    now = datetime.now(UTC)
    await deliver(sessionmaker, sender, now)
    async with sessionmaker() as s:
        await record_result(s, m.id, result(True))
        await record_result(s, m.id, result(True))
    await deliver(sessionmaker, sender, now + timedelta(minutes=1))
    assert [("DOWN" in x.subject, "RECOVERED" in x.subject) for x in sender.sent] == [
        (True, False),
        (False, True),
    ]


async def test_state_transition_notification_is_deduplicated(sessionmaker):
    await open_incident(sessionmaker)
    async with sessionmaker() as s:
        incident_id = (await s.execute(select(Incident.id))).scalar_one()
        await _enqueue_notification(s, incident_id, "opened")
        await _enqueue_notification(s, incident_id, "opened")
        await s.commit()
        assert await s.scalar(select(func.count()).select_from(Notification)) == 1
        await _enqueue_notification(s, incident_id, "resolved")
        await s.commit()
        assert await s.scalar(select(func.count()).select_from(Notification)) == 2


async def test_temporary_failure_is_retried_with_backoff(sessionmaker):
    await open_incident(sessionmaker)
    sender = FakeSender(failures=[TemporaryDeliveryError("SMTP 451"), TemporaryDeliveryError("x")])
    t0 = datetime.now(UTC)

    assert await deliver(sessionmaker, sender, t0) == 1
    n = await only_notification(sessionmaker)
    assert n.status == "pending" and n.attempt_count == 1 and n.last_error == "SMTP 451"
    assert n.next_attempt_at == t0 + timedelta(seconds=30)

    assert await deliver(sessionmaker, sender, t0 + timedelta(seconds=10)) == 0  # not due yet

    assert await deliver(sessionmaker, sender, t0 + timedelta(seconds=31)) == 1
    n = await only_notification(sessionmaker)
    assert n.attempt_count == 2 and n.status == "pending"
    assert n.next_attempt_at == t0 + timedelta(seconds=31 + 60)  # exponential

    assert await deliver(sessionmaker, sender, t0 + timedelta(seconds=200)) == 1
    n = await only_notification(sessionmaker)
    assert n.status == "sent" and n.attempt_count == 3 and n.last_error is None
    assert len(sender.sent) == 1  # exactly one email despite two failed attempts


async def test_retries_stop_after_max_attempts(sessionmaker):
    await open_incident(sessionmaker)
    sender = FakeSender(failures=[TemporaryDeliveryError("down")] * 5)
    t = datetime.now(UTC)
    for _ in range(5):
        await deliver(sessionmaker, sender, t, max_attempts=3)
        t += timedelta(hours=1)
    n = await only_notification(sessionmaker)
    assert n.status == "failed" and n.attempt_count == 3
    assert n.last_error == "down"
    assert sender.sent == []


async def test_permanent_failure_is_not_retried(sessionmaker):
    await open_incident(sessionmaker)
    sender = FakeSender(failures=[PermanentDeliveryError("SMTP 550")])
    t = datetime.now(UTC)
    await deliver(sessionmaker, sender, t)
    await deliver(sessionmaker, sender, t + timedelta(hours=1))
    n = await only_notification(sessionmaker)
    assert n.status == "failed" and n.attempt_count == 1 and n.last_error == "SMTP 550"


async def test_concurrent_workers_send_each_notification_once(sessionmaker):
    for i in range(6):
        await open_incident(sessionmaker, email=f"u{i}@example.com")
    sender = FakeSender()
    sender.delay = 0.05
    now = datetime.now(UTC)
    await asyncio.gather(*(deliver(sessionmaker, sender, now) for _ in range(4)))
    assert len(sender.sent) == 6
    assert len({m.to for m in sender.sent}) == 6


async def test_header_injection_in_monitor_name_is_neutralised(sessionmaker):
    user = await make_user(sessionmaker)
    m = await make_monitor(sessionmaker, user.id, name="evil\r\nBcc: x@y.z", failure_threshold=1)
    async with sessionmaker() as s:
        await record_result(s, m.id, result(False))
    sender = FakeSender()
    await deliver(sessionmaker, sender, datetime.now(UTC))
    assert "\n" not in sender.sent[0].subject and "\r" not in sender.sent[0].subject


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (451, TemporaryDeliveryError),
        (421, TemporaryDeliveryError),
        (550, PermanentDeliveryError),
        (554, PermanentDeliveryError),
    ],
)
async def test_smtp_errors_are_classified(monkeypatch, settings, code, expected):
    async def boom(*a, **k):
        raise aiosmtplib.SMTPResponseException(code, "nope")

    monkeypatch.setattr(notifier.aiosmtplib, "send", boom)
    with pytest.raises(expected):
        await smtp_sender(settings)(Email("a@b.c", "s", "b"))


async def test_connection_failure_is_temporary(monkeypatch, settings):
    async def boom(*a, **k):
        raise aiosmtplib.SMTPConnectError("refused")

    monkeypatch.setattr(notifier.aiosmtplib, "send", boom)
    with pytest.raises(TemporaryDeliveryError):
        await smtp_sender(settings)(Email("a@b.c", "s", "b"))


async def test_smtp_sender_passes_credentials_and_hides_them_from_errors(monkeypatch, settings):
    seen = {}

    async def fake_send(msg, **kw):
        seen.update(kw)
        raise aiosmtplib.SMTPAuthenticationError(535, "bad creds hunter2")

    monkeypatch.setattr(notifier.aiosmtplib, "send", fake_send)
    settings = settings.model_copy(update={"smtp_username": "u", "smtp_password": "hunter2"})
    with pytest.raises(PermanentDeliveryError) as info:
        await smtp_sender(settings)(Email("a@b.c", "s", "b"))
    assert seen["username"] == "u" and seen["password"] == "hunter2"
    assert "hunter2" not in str(info.value)
