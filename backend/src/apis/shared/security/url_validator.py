"""URL validation for outbound HTTP requests.

Provides ``validate_external_url`` which parses a URL, resolves its hostname
via DNS, and rejects targets that resolve to addresses the application has no
business contacting from server-side code:

* Loopback (``127.0.0.0/8``, ``::1``)
* Link-local (``169.254.0.0/16``, ``fe80::/10``) — includes cloud metadata
* Private networks (RFC1918, ``fc00::/7``)
* Multicast, reserved, unspecified
* Cloud metadata-service literal addresses (``169.254.169.254``,
  ``fd00:ec2::254``)

DNS rebinding is mitigated by resolving every result and rejecting the URL if
*any* resolved address is forbidden. Callers can additionally restrict to a
set of allowed domains via ``domain_allowlist``.

Raises :class:`UrlValidationError` for any disallowed input. The error message
is intentionally generic so it's safe to surface to callers without leaking
internal network topology.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Iterable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Cloud metadata-service literal addresses. Belt-and-suspenders alongside the
# link-local check below — these are the ones we most care about.
_METADATA_ADDRESSES: frozenset[str] = frozenset(
    {
        "169.254.169.254",  # AWS / GCP / Azure IMDSv1/v2
        "fd00:ec2::254",  # AWS IMDS over IPv6
        "100.100.100.200",  # Alibaba Cloud
        "169.254.170.2",  # AWS ECS task metadata / credentials provider
    }
)

_DEFAULT_SCHEMES: frozenset[str] = frozenset({"http", "https"})


class UrlValidationError(ValueError):
    """Raised when a URL fails security validation.

    Carries a short, generic message safe to surface to callers. Detailed
    reasons are emitted via the module logger for operator visibility.
    """


def _normalize_host(host: str) -> str:
    """Strip IPv6 brackets and lowercase the hostname."""
    h = host.strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    return h


def _is_forbidden_address(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if the resolved IP must not be contacted."""
    if addr.is_loopback or addr.is_link_local or addr.is_private or addr.is_multicast or addr.is_reserved or addr.is_unspecified:
        return True
    # ``is_private`` covers RFC1918 and ULA, but some address ranges (e.g.
    # carrier-grade NAT 100.64.0.0/10) only land in ``is_private`` in newer
    # Python versions. Add an explicit guard for the CGNAT range so we behave
    # consistently across runtime versions.
    try:
        if isinstance(addr, ipaddress.IPv4Address):
            cgnat = ipaddress.IPv4Network("100.64.0.0/10")
            if addr in cgnat:
                return True
    except ValueError:
        pass
    if str(addr) in _METADATA_ADDRESSES:
        return True
    return False


def _resolve_all(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve every A/AAAA record for *host* and return parsed addresses.

    Raises :class:`UrlValidationError` if resolution fails or yields no results.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        logger.warning("URL validation: DNS resolution failed for host=%r: %s", host, exc)
        raise UrlValidationError("URL host could not be resolved.") from exc

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            addresses.append(ipaddress.ip_address(ip_str))
        except ValueError:
            # Should not occur — getaddrinfo returns numeric IPs — but be defensive.
            logger.warning("URL validation: getaddrinfo returned non-IP %r for host=%r", ip_str, host)
            raise UrlValidationError("URL host resolution returned an invalid address.")

    if not addresses:
        raise UrlValidationError("URL host could not be resolved.")
    return addresses


def validate_external_url(
    url: str,
    *,
    allow_schemes: Iterable[str] = _DEFAULT_SCHEMES,
    domain_allowlist: set[str] | None = None,
) -> str:
    """Validate that *url* is safe to fetch from server-side code.

    Args:
        url: Absolute URL to validate.
        allow_schemes: Iterable of acceptable URL schemes. Defaults to
            ``{"http", "https"}``.
        domain_allowlist: Optional set of fully-qualified hostnames the URL
            must match exactly (case-insensitive). Subdomains are *not*
            implicitly allowed — pass each acceptable host explicitly.

    Returns:
        The original URL string (unmodified) when validation passes.

    Raises:
        UrlValidationError: when validation fails. The exception message is
            intentionally generic; details are logged.
    """
    if not isinstance(url, str) or not url.strip():
        raise UrlValidationError("URL is required.")

    schemes = {s.lower() for s in allow_schemes}

    try:
        parsed = urlparse(url.strip())
    except (ValueError, AttributeError) as exc:
        raise UrlValidationError("URL could not be parsed.") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in schemes:
        logger.warning("URL validation: rejected scheme=%r for url=%r", scheme, url)
        raise UrlValidationError("URL scheme is not permitted.")

    host = parsed.hostname
    if not host:
        raise UrlValidationError("URL is missing a host.")

    host = _normalize_host(host)

    # Reject embedded credentials — they're never appropriate for a backend-
    # initiated request to an internet endpoint.
    if parsed.username or parsed.password:
        logger.warning("URL validation: rejected URL with embedded credentials")
        raise UrlValidationError("URL must not contain embedded credentials.")

    if domain_allowlist is not None:
        normalized_allow = {_normalize_host(h) for h in domain_allowlist}
        if host not in normalized_allow:
            logger.warning(
                "URL validation: host=%r not in allowlist (size=%d)",
                host,
                len(normalized_allow),
            )
            raise UrlValidationError("URL host is not in the allowed domains.")

    # If the host is itself an IP literal, validate it directly without DNS.
    try:
        literal_addr = ipaddress.ip_address(host)
    except ValueError:
        literal_addr = None

    if literal_addr is not None:
        if _is_forbidden_address(literal_addr):
            logger.warning("URL validation: rejected literal address=%s", literal_addr)
            raise UrlValidationError("URL host is not permitted.")
        return url

    # Otherwise, resolve and check every result.
    addresses = _resolve_all(host)
    for addr in addresses:
        if _is_forbidden_address(addr):
            logger.warning(
                "URL validation: host=%r resolved to forbidden address=%s (one of %d results)",
                host,
                addr,
                len(addresses),
            )
            raise UrlValidationError("URL host is not permitted.")

    return url
