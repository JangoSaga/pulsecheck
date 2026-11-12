"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-12
"""

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_table(
        "monitors",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "user_id", sa.Uuid, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("target_url", sa.String(2048), nullable=False),
        sa.Column("http_method", sa.String(8), nullable=False),
        sa.Column("interval_seconds", sa.Integer, nullable=False),
        sa.Column("timeout_seconds", sa.Integer, nullable=False),
        sa.Column("expected_status_codes", sa.JSON, nullable=False),
        sa.Column("failure_threshold", sa.Integer, nullable=False),
        sa.Column("recovery_threshold", sa.Integer, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("consecutive_failures", sa.Integer, nullable=False),
        sa.Column("consecutive_successes", sa.Integer, nullable=False),
        sa.Column("last_checked_at", TS),
        sa.Column("next_check_at", TS, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_index("ix_monitors_user_id", "monitors", ["user_id"])
    op.create_index(
        "ix_monitors_active_next_check",
        "monitors",
        ["next_check_at"],
        postgresql_where=sa.text("is_active"),
    )
    op.create_table(
        "monitor_checks",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "monitor_id", sa.Uuid, sa.ForeignKey("monitors.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("status_code", sa.Integer),
        sa.Column("response_time_ms", sa.Integer),
        sa.Column("error_type", sa.String(32)),
        sa.Column("error_message", sa.String(255)),
        sa.Column("checked_at", TS, nullable=False),
    )
    op.create_index(
        "ix_monitor_checks_monitor_checked", "monitor_checks", ["monitor_id", "checked_at"]
    )
    op.create_index("ix_monitor_checks_checked_at", "monitor_checks", ["checked_at"])
    op.create_table(
        "incidents",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "monitor_id", sa.Uuid, sa.ForeignKey("monitors.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("resolved_at", TS),
        sa.Column("failure_reason", sa.String(255), nullable=False),
    )
    op.create_index("ix_incidents_monitor_started", "incidents", ["monitor_id", "started_at"])
    op.create_index(
        "uq_incidents_one_open_per_monitor",
        "incidents",
        ["monitor_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "incident_id",
            sa.Uuid,
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("event", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt_count", sa.Integer, nullable=False),
        sa.Column("last_error", sa.Text),
        sa.Column("next_attempt_at", TS, nullable=False),
        sa.Column("sent_at", TS),
        sa.Column("created_at", TS, nullable=False),
        sa.UniqueConstraint("incident_id", "channel", "event", name="uq_notification_transition"),
    )
    op.create_index(
        "ix_notifications_due",
        "notifications",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("notifications")
    op.drop_table("incidents")
    op.drop_table("monitor_checks")
    op.drop_table("monitors")
    op.drop_table("users")
