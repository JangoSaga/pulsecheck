import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

HttpMethod = Literal["GET", "HEAD", "POST"]


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)

    @field_validator("password")
    @classmethod
    def _bcrypt_limit(cls, v: str) -> str:
        if len(v.encode()) > 72:
            raise ValueError("password must be at most 72 bytes")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=72)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    created_at: datetime


def _check_name(v: str) -> str:
    v = v.strip()
    if not v or _CONTROL.search(v):
        raise ValueError("name must be non-empty and contain no control characters")
    return v


def _check_codes(v: list[int]) -> list[int]:
    if not v or len(v) > 20:
        raise ValueError("expected_status_codes must contain 1 to 20 codes")
    if any(c < 100 or c > 599 for c in v):
        raise ValueError("status codes must be between 100 and 599")
    return sorted(set(v))


class MonitorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    target_url: str = Field(max_length=2048)
    http_method: HttpMethod = "GET"
    interval_seconds: int = Field(default=60, ge=1, le=86400)
    timeout_seconds: int = Field(default=10, ge=1, le=60)
    expected_status_codes: list[int] = Field(default_factory=lambda: [200])
    failure_threshold: int = Field(default=3, ge=1, le=20)
    recovery_threshold: int = Field(default=2, ge=1, le=20)

    _name = field_validator("name")(_check_name)
    _codes = field_validator("expected_status_codes")(_check_codes)

    @model_validator(mode="after")
    def _timeout_within_interval(self) -> "MonitorCreate":
        if self.timeout_seconds > self.interval_seconds:
            raise ValueError("timeout_seconds must not exceed interval_seconds")
        return self


class MonitorUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    target_url: str | None = Field(default=None, max_length=2048)
    http_method: HttpMethod | None = None
    interval_seconds: int | None = Field(default=None, ge=1, le=86400)
    timeout_seconds: int | None = Field(default=None, ge=1, le=60)
    expected_status_codes: list[int] | None = None
    failure_threshold: int | None = Field(default=None, ge=1, le=20)
    recovery_threshold: int | None = Field(default=None, ge=1, le=20)

    @field_validator("name")
    @classmethod
    def _n(cls, v: str | None) -> str | None:
        return _check_name(v) if v is not None else v

    @field_validator("expected_status_codes")
    @classmethod
    def _c(cls, v: list[int] | None) -> list[int] | None:
        return _check_codes(v) if v is not None else v


class MonitorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    target_url: str
    http_method: str
    interval_seconds: int
    timeout_seconds: int
    expected_status_codes: list[int]
    failure_threshold: int
    recovery_threshold: int
    is_active: bool
    status: str
    consecutive_failures: int
    last_checked_at: datetime | None
    next_check_at: datetime
    created_at: datetime
    updated_at: datetime


class CheckOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    outcome: str
    status_code: int | None
    response_time_ms: int | None
    error_type: str | None
    error_message: str | None
    checked_at: datetime


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel: str
    event: str
    status: str
    attempt_count: int
    last_error: str | None
    sent_at: datetime | None
    created_at: datetime


class IncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    monitor_id: uuid.UUID
    status: str
    started_at: datetime
    resolved_at: datetime | None
    failure_reason: str
    notifications: list[NotificationOut] = []


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


class UptimeOut(BaseModel):
    monitor_id: uuid.UUID
    monitor_name: str
    current_status: str
    is_active: bool
    start: datetime
    end: datetime
    total_checks: int
    successful_checks: int
    failed_checks: int
    uptime_percentage: float | None
    expected_checks: int
    coverage_percentage: float | None
    avg_response_time_ms: float | None
    p50_response_time_ms: float | None
    p95_response_time_ms: float | None
    p99_response_time_ms: float | None
    incident_count: int
    total_incident_seconds: float


class UptimeResponse(BaseModel):
    start: datetime
    end: datetime
    monitors: list[UptimeOut]
