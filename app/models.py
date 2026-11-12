import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Monitor(Base):
    __tablename__ = "monitors"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    target_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    http_method: Mapped[str] = mapped_column(String(8), nullable=False, default="GET")
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_status_codes: Mapped[list[int]] = mapped_column(
        JSON, nullable=False, default=lambda: [200]
    )
    failure_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    recovery_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)

    # Runtime state, maintained by the check pipeline.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_successes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        Index("ix_monitors_user_id", "user_id"),
        # Scheduler claim query: active monitors ordered by due time.
        Index(
            "ix_monitors_active_next_check",
            "next_check_at",
            postgresql_where=text("is_active"),
        ),
    )


class MonitorCheck(Base):
    __tablename__ = "monitor_checks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    monitor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("monitors.id", ondelete="CASCADE"), nullable=False
    )
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    response_time_ms: Mapped[int | None] = mapped_column(Integer)
    error_type: Mapped[str | None] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(String(255))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_monitor_checks_monitor_checked", "monitor_id", "checked_at"),
        Index("ix_monitor_checks_checked_at", "checked_at"),
    )


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    monitor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("monitors.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str] = mapped_column(String(255), nullable=False)

    notifications: Mapped[list["Notification"]] = relationship(back_populates="incident")

    __table_args__ = (
        Index("ix_incidents_monitor_started", "monitor_id", "started_at"),
        # Open-incident lookup and a hard guarantee of at most one open incident per monitor.
        Index(
            "uq_incidents_one_open_per_monitor",
            "monitor_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="email")
    event: Mapped[str] = mapped_column(String(16), nullable=False)  # opened | resolved
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    incident: Mapped[Incident] = relationship(back_populates="notifications")

    __table_args__ = (
        # One notification per state transition per channel: the dedup guarantee.
        UniqueConstraint("incident_id", "channel", "event", name="uq_notification_transition"),
        Index(
            "ix_notifications_due",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )
