"""Single health-check service: one HTTP request, one classified result.

The guard in ssrf.py runs here, at request time, on every check. Redirects are never
followed: a 3xx response is just a status code compared with the expected codes.
"""

import asyncio
import ssl
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from app.services.ssrf import (
    InvalidTargetError,
    Resolver,
    UnsafeTargetError,
    parse_target,
    resolve_safe,
)

USER_AGENT = "PulseCheck/1.0 (+uptime-monitor)"

HEALTHY = "healthy"
UNHEALTHY = "unhealthy"


@dataclass(frozen=True)
class CheckResult:
    outcome: str
    status_code: int | None
    response_time_ms: int | None
    error_type: str | None
    error_message: str | None
    checked_at: datetime

    @property
    def healthy(self) -> bool:
        return self.outcome == HEALTHY

    @property
    def failure_reason(self) -> str:
        if self.error_type == "unexpected_status":
            return f"Unexpected status code {self.status_code}"
        return f"{self.error_type}: {self.error_message}"


def _unhealthy(
    error_type: str,
    message: str,
    started: float,
    status_code: int | None = None,
    with_time: bool = False,
) -> CheckResult:
    # Messages are fixed strings on purpose: raw exception text can carry URLs,
    # addresses or headers.
    return CheckResult(
        outcome=UNHEALTHY,
        status_code=status_code,
        response_time_ms=int((time.perf_counter() - started) * 1000) if with_time else None,
        error_type=error_type,
        error_message=message[:255],
        checked_at=datetime.now(UTC),
    )


async def perform_check(
    url: str,
    method: str,
    timeout_seconds: float,
    expected_status_codes: list[int],
    *,
    allow_private: bool = False,
    resolver: Resolver | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CheckResult:
    started = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_seconds):
            return await _run(
                url,
                method,
                timeout_seconds,
                expected_status_codes,
                allow_private,
                resolver,
                transport,
                started,
            )
    except TimeoutError:
        return _unhealthy("timeout", "Request timed out", started, with_time=True)


async def _run(
    url: str,
    method: str,
    timeout_seconds: float,
    expected: list[int],
    allow_private: bool,
    resolver: Resolver | None,
    transport: httpx.AsyncBaseTransport | None,
    started: float,
) -> CheckResult:
    try:
        target = parse_target(url)
    except InvalidTargetError:
        return _unhealthy("invalid_url", "Target URL is invalid", started)

    try:
        ip = await resolve_safe(target, allow_private=allow_private, resolver=resolver)
    except UnsafeTargetError:
        return _unhealthy("ssrf_blocked", "Destination is not an allowed target", started)
    except OSError:
        return _unhealthy("dns_error", "Host name could not be resolved", started)

    # Connect to the validated IP, keep the original Host header and TLS server name.
    pinned = httpx.URL(url).copy_with(host=str(ip))
    host_header = target.host if target.port in (80, 443) else f"{target.host}:{target.port}"
    request_ext = {"sni_hostname": target.host} if target.scheme == "https" else {}
    timeout = httpx.Timeout(timeout_seconds)

    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        ) as client:
            request = client.build_request(
                method,
                pinned,
                headers={"Host": host_header, "User-Agent": USER_AGENT},
                extensions=request_ext,
            )
            response = await client.send(request, stream=True)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            status = response.status_code
            await response.aclose()
    except httpx.TimeoutException:
        return _unhealthy("timeout", "Request timed out", started, with_time=True)
    except httpx.ConnectError as exc:
        if (
            isinstance(exc.__cause__, ssl.SSLError)
            or "SSL" in str(exc)
            or "CERTIFICATE" in str(exc)
        ):
            return _unhealthy("tls_error", "TLS handshake failed", started)
        return _unhealthy("connection_error", "Connection failed", started)
    except httpx.HTTPError:
        return _unhealthy("connection_error", "Request failed", started)

    checked_at = datetime.now(UTC)
    if status in expected:
        return CheckResult(HEALTHY, status, elapsed_ms, None, None, checked_at)
    return CheckResult(
        UNHEALTHY,
        status,
        elapsed_ms,
        "unexpected_status",
        f"Status {status} not in expected codes",
        checked_at,
    )
