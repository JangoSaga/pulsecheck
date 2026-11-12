import ipaddress

import httpx
import pytest

from app.services.checker import perform_check
from app.services.ssrf import (
    InvalidTargetError,
    UnsafeTargetError,
    is_ip_allowed,
    parse_target,
    resolve_safe,
    validate_target_url,
)
from tests.conftest import PUBLIC_IP, fake_resolver

BLOCKED = [
    "127.0.0.1",
    "127.1.2.3",
    "0.0.0.0",
    "10.0.0.1",
    "172.16.5.4",
    "172.31.255.255",
    "192.168.0.1",
    "169.254.169.254",
    "169.254.1.1",
    "100.64.0.1",
    "100.100.100.200",
    "192.0.0.192",
    "224.0.0.1",
    "255.255.255.255",
    "::1",
    "::",
    "fe80::1",
    "fc00::1",
    "fd00:ec2::254",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    "::ffff:169.254.169.254",
    "64:ff9b::1",
]


@pytest.mark.parametrize("ip", BLOCKED)
def test_blocked_addresses(ip):
    assert not is_ip_allowed(ipaddress.ip_address(ip))


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses_allowed(ip):
    assert is_ip_allowed(ipaddress.ip_address(ip))


@pytest.mark.parametrize(
    "ip", ["169.254.169.254", "169.254.170.2", "fd00:ec2::254", "::ffff:169.254.169.254"]
)
def test_metadata_blocked_even_when_private_allowed(ip):
    assert not is_ip_allowed(ipaddress.ip_address(ip), allow_private=True)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.1.1", "192.168.1.1", "::1"])
def test_private_allowed_only_with_override(ip):
    assert not is_ip_allowed(ipaddress.ip_address(ip))
    assert is_ip_allowed(ipaddress.ip_address(ip), allow_private=True)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/",
        "file:///etc/passwd",
        "gopher://example.com/",
        "javascript:alert(1)",
        "http://user:pass@example.com/",
        "http://:80/",
        "http:///path",
        "example.com",
        "",
        "http://exa mple.com/",
        "http://example.com:99999/",
        "http://example.com/\r\nHost: evil",
        "https://" + "a" * 3000,
    ],
)
def test_invalid_urls_rejected(url):
    with pytest.raises(InvalidTargetError):
        parse_target(url)


@pytest.mark.parametrize(
    "url",
    ["http://example.com", "https://example.com:8443/a?b=1", "http://sub.example.co.uk/x"],
)
def test_valid_urls_parse(url):
    target = parse_target(url)
    assert target.host and target.port


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://LOCALHOST./",
        "http://foo.localhost/",
        "http://10.0.0.1:8080/",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fd00:ec2::254]/",
        "http://internal.example/",
        "http://loopback.example/",
        "http://metadata.example/",
        "http://mixed.example/",
    ],
)
async def test_unsafe_urls_rejected(url):
    with pytest.raises(UnsafeTargetError):
        await validate_target_url(url, resolver=fake_resolver)


async def test_decimal_and_hex_ip_forms_are_blocked():
    # getaddrinfo normalises these to 127.0.0.1; the resolved address is what is checked.
    async def resolver(host, port):
        return {"2130706433": ["127.0.0.1"], "0x7f000001": ["127.0.0.1"]}[host]

    for host in ("2130706433", "0x7f000001"):
        with pytest.raises(UnsafeTargetError):
            await validate_target_url(f"http://{host}/", resolver=resolver)


async def test_unresolvable_host_accepted_at_validation():
    target = await validate_target_url("http://nxdomain.example/", resolver=fake_resolver)
    assert target.host == "nxdomain.example"


async def test_dns_is_resolved_again_for_every_check():
    """DNS rebinding: public on validation, private at request time."""
    calls = []

    async def rebinding(host, port):
        calls.append(host)
        return [PUBLIC_IP] if len(calls) == 1 else ["127.0.0.1"]

    await validate_target_url("http://rebind.example/", resolver=rebinding)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    res = await perform_check(
        "http://rebind.example/",
        "GET",
        5,
        [200],
        resolver=rebinding,
        transport=httpx.MockTransport(handler),
    )
    assert len(calls) == 2
    assert res.outcome == "unhealthy" and res.error_type == "ssrf_blocked"
    assert seen == []  # no request was ever sent


async def test_connection_is_pinned_to_the_validated_ip():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    res = await perform_check(
        "https://example.com:8443/health",
        "GET",
        5,
        [200],
        resolver=fake_resolver,
        transport=httpx.MockTransport(handler),
    )
    assert res.healthy
    req = seen[0]
    assert req.url.host == PUBLIC_IP  # connected by IP, not by name
    assert req.headers["host"] == "example.com:8443"
    assert req.extensions["sni_hostname"] == "example.com"


async def test_mixed_answers_are_rejected():
    with pytest.raises(UnsafeTargetError):
        await resolve_safe(parse_target("http://mixed.example/"), resolver=fake_resolver)


async def test_redirects_are_not_followed():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/"})

    res = await perform_check(
        "https://example.com/",
        "GET",
        5,
        [200],
        resolver=fake_resolver,
        transport=httpx.MockTransport(handler),
    )
    assert len(seen) == 1
    assert res.outcome == "unhealthy"
    assert res.status_code == 302
    assert res.error_type == "unexpected_status"


async def test_redirect_status_can_be_expected_explicitly():
    def handler(request):
        return httpx.Response(301, headers={"Location": "http://10.0.0.1/"})

    res = await perform_check(
        "https://example.com/",
        "GET",
        5,
        [301],
        resolver=fake_resolver,
        transport=httpx.MockTransport(handler),
    )
    assert res.healthy and res.status_code == 301


async def test_local_server_redirect_not_followed(local_server):
    res = await perform_check(local_server.url("/redirect"), "GET", 5, [200], allow_private=True)
    assert res.status_code == 302
    assert local_server.hits == ["/redirect"]


async def test_check_blocks_private_target_without_override(local_server):
    res = await perform_check(local_server.url("/status/200"), "GET", 5, [200])
    assert res.error_type == "ssrf_blocked"
    assert local_server.hits == []


async def test_check_to_metadata_blocked_even_with_override():
    res = await perform_check("http://169.254.169.254/latest/", "GET", 5, [200], allow_private=True)
    assert res.error_type == "ssrf_blocked"


async def test_blocked_error_message_does_not_leak_address():
    res = await perform_check("http://10.1.2.3/secret", "GET", 5, [200])
    assert "10.1.2.3" not in (res.error_message or "")
    assert "secret" not in (res.error_message or "")
