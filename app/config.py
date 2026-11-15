from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_JWT_SECRET = "dev-insecure-secret-change-me-0123456789"  # noqa: S105


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://pulsecheck:pulsecheck@localhost:5433/pulsecheck"
    redis_url: str = "redis://localhost:6380/0"

    jwt_secret: str = DEV_JWT_SECRET
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60
    bcrypt_rounds: int = 12

    # SSRF guard. Only enable in a controlled development environment.
    allow_private_targets: bool = False

    # Monitor limits
    min_interval_seconds: int = 10
    max_interval_seconds: int = 86400
    max_timeout_seconds: int = 60
    max_monitors_per_user: int = 50

    # Scheduler and workers
    scheduler_tick_seconds: float = 2.0
    scheduler_batch_size: int = 200
    max_concurrent_checks: int = 20
    stale_job_seconds: int = 180
    heartbeat_ttl_seconds: int = 30

    # Retention
    retention_days: int = 30
    retention_interval_seconds: int = 3600
    retention_batch_size: int = 5000

    # Notifications
    notify_max_attempts: int = 5
    notify_retry_base_seconds: int = 30
    notify_poll_seconds: float = 2.0
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_starttls: bool = False
    smtp_use_tls: bool = False
    smtp_timeout_seconds: float = 10.0
    mail_from: str = "PulseCheck <alerts@pulsecheck.local>"

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, v: str) -> str:
        # Render (and Heroku-style hosts) provide postgres:// or postgresql://.
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+asyncpg://" + v[len(prefix) :]
        return v

    @model_validator(mode="after")
    def _reject_unsafe_production(self) -> "Settings":
        if self.environment == "production":
            if self.jwt_secret == DEV_JWT_SECRET or len(self.jwt_secret) < 32:
                raise ValueError("JWT_SECRET must be set to a strong value in production")
            if self.allow_private_targets:
                raise ValueError("ALLOW_PRIVATE_TARGETS must be false in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
