import time

import httpx
import pytest

from app.services.checker import perform_check
from tests.conftest import fake_resolver


def mock(handler):
    return httpx.MockTransport(handler)


async def check(handler, expected=(200,), method="GET", timeout=5, url="https://example.com/h"):
    return await perform_check(
        url, method, timeout, list(expected), resolver=fake_resolver, transport=mock(handler)
    )


async def test_successful_check():
    res = await check(lambda r: httpx.Response(200))
    assert res.outcome == "healthy"
    assert res.status_code == 200
    assert res.error_type is None
    assert res.response_time_ms is not None and res.response_time_ms >= 0


async def test_failing_status_is_unhealthy():
    res = await check(lambda r: httpx.Response(503))
    assert res.outcome == "unhealthy"
    assert res.status_code == 503
    assert res.error_type == "unexpected_status"
    assert "503" in res.failure_reason


@pytest.mark.parametrize(
    ("status", "expected", "healthy"),
    [
        (200, [200], True),
        (201, [200], False),
        (201, [200, 201], True),
        (204, [200, 201, 204], True),
        (404, [200], False),
        (404, [404], True),  # a monitor may legitimately expect a 404
        (500, [200, 201], False),
        (301, [200], False),
    ],
)
async def test_unexpected_status_codes(status, expected, healthy):
    res = await check(lambda r: httpx.Response(status), expected=expected)
    assert res.healthy is healthy
    assert res.status_code == status


async def test_connection_error():
    def handler(request):
        raise httpx.ConnectError("boom to 10.1.1.1 secret")

    res = await check(handler)
    assert res.outcome == "unhealthy"
    assert res.error_type == "connection_error"
    assert res.status_code is None
    assert "10.1.1.1" not in res.error_message


async def test_timeout_from_transport():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    res = await check(handler)
    assert res.error_type == "timeout"
    assert res.outcome == "unhealthy"


async def test_unresolvable_host():
    res = await perform_check(
        "https://nxdomain.example/",
        "GET",
        5,
        [200],
        resolver=fake_resolver,
        transport=mock(lambda r: httpx.Response(200)),
    )
    assert res.error_type == "dns_error"


async def test_http_method_is_used():
    seen = []

    def handler(request):
        seen.append(request.method)
        return httpx.Response(200)

    await check(handler, method="HEAD")
    await check(handler, method="POST")
    assert seen == ["HEAD", "POST"]


async def test_real_timeout_against_slow_local_server(local_server):
    started = time.perf_counter()
    res = await perform_check(local_server.url("/delay/3"), "GET", 1, [200], allow_private=True)
    elapsed = time.perf_counter() - started
    assert res.error_type == "timeout"
    assert elapsed < 2.5  # bounded by the configured timeout, not the server delay


async def test_real_success_and_failure_against_local_server(local_server):
    ok = await perform_check(local_server.url("/status/200"), "GET", 5, [200], allow_private=True)
    bad = await perform_check(local_server.url("/status/500"), "GET", 5, [200], allow_private=True)
    assert ok.healthy and ok.response_time_ms is not None
    assert bad.outcome == "unhealthy" and bad.status_code == 500


async def test_connection_refused_on_closed_port():
    res = await perform_check("http://127.0.0.1:1/", "GET", 3, [200], allow_private=True)
    assert res.error_type == "connection_error"


async def test_invalid_url_is_unhealthy_not_exception():
    res = await perform_check("ftp://example.com", "GET", 3, [200])
    assert res.error_type == "invalid_url"
