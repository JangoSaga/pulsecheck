"""SSRF guard.

Rules:
* Only http and https, no embedded credentials, a real hostname or IP literal.
* Every resolved address must be globally routable. Private, loopback, link-local,
  carrier-grade NAT, multicast, reserved, unspecified and cloud metadata addresses are
  rejected. IPv4-mapped IPv6 addresses are unwrapped before the check.
* DNS is resolved again at request time and the connection is pinned to the validated
  IP, so a hostname cannot change its answer between validation and connection
  (DNS rebinding).
* ``allow_private`` relaxes the private/loopback/link-local rules for controlled
  development only. Cloud metadata addresses stay blocked even then.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]

ALLOWED_SCHEMES = {"http", "https"}
MAX_URL_LENGTH = 2048

# Always blocked, even with allow_private.
METADATA_ADDRESSES = {
    ipaddress.ip_address("169.254.169.254"),  # AWS, GCP, Azure, DigitalOcean, OpenStack
    ipaddress.ip_address("169.254.170.2"),  # AWS ECS task metadata
    ipaddress.ip_address("169.254.169.253"),  # AWS DNS
    ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud
    ipaddress.ip_address("192.0.0.192"),  # Oracle Cloud
    ipaddress.ip_address("fd00:ec2::254"),  # AWS IMDS over IPv6
}
BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal", "metadata"}
BLOCKED_SUFFIXES = (".localhost", ".internal", ".local")


class InvalidTargetError(ValueError):
    """The URL is malformed or uses a disallowed scheme or form."""


class UnsafeTargetError(ValueError):
    """The URL points at a destination that is not allowed."""


@dataclass(frozen=True)
class Target:
    url: str
    scheme: str
    host: str
    port: int


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _unwrap(ip: IPAddress) -> IPAddress:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip.sixtofour is not None:
            return ip.sixtofour
        if ip in _NAT64:
            # NAT64 well-known prefix embeds an IPv4 address that a gateway would reach.
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        teredo = ip.teredo
        if teredo is not None:
            return teredo[1]
    return ip


def is_ip_allowed(ip: IPAddress, allow_private: bool = False) -> bool:
    ip = _unwrap(ip)
    if ip in METADATA_ADDRESSES:
        return False
    if allow_private:
        # Controlled development: private, loopback and link-local space is permitted.
        return not (ip.is_multicast or ip.is_unspecified)
    return ip.is_global and not ip.is_multicast


def parse_target(url: str) -> Target:
    """Syntactic validation. Does no network I/O."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        raise InvalidTargetError("URL is empty or too long")
    if any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise InvalidTargetError("URL contains whitespace or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise InvalidTargetError("URL is malformed") from exc
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidTargetError("Only http and https URLs are allowed")
    if parts.username is not None or parts.password is not None:
        raise InvalidTargetError("Credentials in URLs are not allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise InvalidTargetError("URL has no host")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidTargetError("Host name is invalid") from exc
    if port is not None and not 1 <= port <= 65535:
        raise InvalidTargetError("Port is out of range")
    return Target(
        url=url,
        scheme=scheme,
        host=host,
        port=port or (443 if scheme == "https" else 80),
    )


def _is_ip_literal(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.append(addr)
    return seen


async def resolve_safe(
    target: Target,
    *,
    allow_private: bool = False,
    resolver: Resolver | None = None,
) -> IPAddress:
    """Resolve the host now and return the single validated IP to connect to.

    Every address the name resolves to must be allowed. Raises UnsafeTargetError
    otherwise, and OSError subclasses when DNS fails.
    """
    host = target.host
    named_local = host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES)
    if named_local and (not allow_private or host in {"metadata.google.internal", "metadata"}):
        raise UnsafeTargetError("Host name is not allowed")

    literal = _is_ip_literal(host)
    if literal is not None:
        addresses: list[IPAddress] = [literal]
    else:
        raw = await (resolver or system_resolver)(host, target.port)
        if not raw:
            raise OSError("host did not resolve")
        addresses = []
        for item in raw:
            try:
                addresses.append(ipaddress.ip_address(item.split("%")[0]))
            except ValueError as exc:
                raise UnsafeTargetError("Resolver returned an invalid address") from exc

    for ip in addresses:
        if not is_ip_allowed(ip, allow_private):
            raise UnsafeTargetError("Destination address is not allowed")
    ipv4 = [a for a in addresses if a.version == 4]
    return (ipv4 or addresses)[0]


async def validate_target_url(
    url: str,
    *,
    allow_private: bool = False,
    resolver: Resolver | None = None,
) -> Target:
    """Validation used when a monitor is created or updated.

    Rejects malformed and unsafe URLs. A name that does not resolve yet is accepted:
    the check itself will report a dns_error, and the guard runs again at request time.
    """
    target = parse_target(url)
    try:
        await resolve_safe(target, allow_private=allow_private, resolver=resolver)
    except UnsafeTargetError:
        raise
    except OSError:
        pass
    return target
