"""Notification delivery with retry.

Rows are created by the state machine (one per incident transition, enforced by a unique
constraint). This module sends the pending ones:

* due rows are claimed with FOR UPDATE SKIP LOCKED, so workers never send the same row
  concurrently;
* a temporary failure (connection error, timeout, SMTP 4xx) schedules a retry with
  exponential backoff (base * 2^(attempt-1)) until NOTIFY_MAX_ATTEMPTS, then the row is
  marked failed;
* a permanent failure (SMTP 5xx) marks the row failed immediately;
* delivery is at-least-once: a crash after the SMTP accept but before commit can resend.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage

import aiosmtplib
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import Incident, Monitor, Notification, User

log = logging.getLogger(__name__)


class TemporaryDeliveryError(Exception):
    pass


class PermanentDeliveryError(Exception):
    pass


@dataclass(frozen=True)
class Email:
    to: str
    subject: str
    body: str


Sender = Callable[[Email], Awaitable[None]]


def _clean(text: str) -> str:
    return "".join(c for c in text if c.isprintable())


def build_email(notification: Notification, incident: Incident, monitor: Monitor, to: str) -> Email:
    name = _clean(monitor.name)
    if notification.event == "opened":
        subject = f"[PulseCheck] DOWN: {name}"
        body = (
            f"Monitor '{name}' is down.\n\n"
            f"Target: {monitor.target_url}\n"
            f"Started: {incident.started_at.isoformat()}\n"
            f"Reason: {incident.failure_reason}\n"
        )
    else:
        subject = f"[PulseCheck] RECOVERED: {name}"
        resolved = incident.resolved_at.isoformat() if incident.resolved_at else "unknown"
        body = (
            f"Monitor '{name}' has recovered.\n\n"
            f"Target: {monitor.target_url}\n"
            f"Started: {incident.started_at.isoformat()}\n"
            f"Resolved: {resolved}\n"
        )
    return Email(to=to, subject=subject, body=body)


def smtp_sender(settings: Settings) -> Sender:
    async def send(mail: Email) -> None:
        msg = EmailMessage()
        msg["From"] = settings.mail_from
        msg["To"] = mail.to
        msg["Subject"] = mail.subject
        msg.set_content(mail.body)
        try:
            await aiosmtplib.send(
                msg,
                hostname=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_username or None,
                password=settings.smtp_password or None,
                start_tls=settings.smtp_starttls,
                use_tls=settings.smtp_use_tls,
                timeout=settings.smtp_timeout_seconds,
            )
        except aiosmtplib.SMTPResponseException as exc:
            if 500 <= exc.code < 600:
                raise PermanentDeliveryError(f"SMTP {exc.code}") from exc
            raise TemporaryDeliveryError(f"SMTP {exc.code}") from exc
        except (aiosmtplib.SMTPException, OSError, TimeoutError) as exc:
            raise TemporaryDeliveryError(type(exc).__name__) from exc

    return send


def backoff_seconds(attempt: int, base: int) -> int:
    return base * (2 ** (attempt - 1))


async def deliver_due_notifications(
    sessionmaker: async_sessionmaker[AsyncSession],
    sender: Sender,
    now: datetime,
    *,
    max_attempts: int,
    retry_base_seconds: int,
    batch_size: int = 20,
) -> int:
    """Attempt delivery for due notifications. Returns the number attempted."""
    attempted = 0
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(Notification, Incident, Monitor, User.email)
                .join(Incident, Incident.id == Notification.incident_id)
                .join(Monitor, Monitor.id == Incident.monitor_id)
                .join(User, User.id == Monitor.user_id)
                .where(Notification.status == "pending", Notification.next_attempt_at <= now)
                .order_by(Notification.next_attempt_at)
                .limit(batch_size)
                .with_for_update(of=Notification, skip_locked=True)
            )
        ).all()
        for notification, incident, monitor, email in rows:
            attempted += 1
            notification.attempt_count += 1
            try:
                await sender(build_email(notification, incident, monitor, email))
            except PermanentDeliveryError as exc:
                notification.status = "failed"
                notification.last_error = str(exc)[:500]
            except TemporaryDeliveryError as exc:
                notification.last_error = str(exc)[:500]
                if notification.attempt_count >= max_attempts:
                    notification.status = "failed"
                else:
                    delay = backoff_seconds(notification.attempt_count, retry_base_seconds)
                    notification.next_attempt_at = now + timedelta(seconds=delay)
            else:
                notification.status = "sent"
                notification.sent_at = now
                notification.last_error = None
            log.info(
                "notification attempt",
                extra={
                    "notification_id": str(notification.id),
                    "event": notification.event,
                    "status": notification.status,
                    "attempt": notification.attempt_count,
                },
            )
        await session.commit()
    return attempted
