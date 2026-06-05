"""Unit tests for ``apis.shared.security.url_validator``.

These tests assert the *positive invariant* that the validator exists to
enforce: server-side outbound HTTP requests must not target loopback,
link-local (including cloud metadata), private, multicast, reserved, or
otherwise unroutable addresses, and must defeat DNS-rebinding.
"""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from apis.shared.security.url_validator import (
    UrlValidationError,
    validate_external_url,
)


def _fake_getaddrinfo(host_to_addrs: dict[str, list[str]]):
    """Build a getaddrinfo replacement that returns canned IPs per host."""

    def _impl(host, *args, **kwargs):
        if host not in host_to_addrs:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        results = []
        for ip in host_to_addrs[host]:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            results.append((family, socket.SOCK_STREAM, 0, "", (ip, 0)))
        return results

    return _impl


# ---- IP-literal hosts: validated directly without DNS ---------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.1:8080/foo",
        "https://localhost.localdomain/x",
        "http://[::1]/",
    ],
)
def test_loopback_addresses_rejected(url: str) -> None:
    # localhost.localdomain isn't an IP literal; rely on DNS resolution
    # returning a loopback address. For the IP-literal cases the validator
    # should reject without DNS at all.
    if "localhost.localdomain" in url:
        with patch(
            "apis.shared.security.url_validator.socket.getaddrinfo",
            _fake_getaddrinfo({"localhost.localdomain": ["127.0.0.1"]}),
        ):
            with pytest.raises(UrlValidationError):
                validate_external_url(url)
    else:
        with pytest.raises(UrlValidationError):
            validate_external_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://169.254.169.254:80/",
        "http://[fd00:ec2::254]/latest/meta-data/",
        "http://[fe80::1]/",
    ],
)
def test_link_local_and_metadata_rejected(url: str) -> None:
    with pytest.raises(UrlValidationError):
        validate_external_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1/",
        "http://10.255.255.255/",
        "http://172.16.0.1/",
        "http://172.31.255.254/",
        "http://192.168.1.1/",
        "http://[fc00::1]/",
        "http://[fd00::1]/",
    ],
)
def test_rfc1918_and_ula_rejected(url: str) -> None:
    with pytest.raises(UrlValidationError):
        validate_external_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://224.0.0.1/",  # multicast IPv4
        "http://[ff02::1]/",  # multicast IPv6
        "http://0.0.0.0/",  # unspecified
        "http://100.64.0.1/",  # carrier-grade NAT
        "http://169.254.170.2/",  # ECS task metadata literal
    ],
)
def test_other_unroutable_ranges_rejected(url: str) -> None:
    with pytest.raises(UrlValidationError):
        validate_external_url(url)


# ---- DNS resolution + rebinding defense -----------------------------------


def test_public_url_resolved_to_public_ip_passes() -> None:
    fake = _fake_getaddrinfo({"example.com": ["93.184.216.34"]})
    with patch("apis.shared.security.url_validator.socket.getaddrinfo", fake):
        assert validate_external_url("https://example.com/path") == "https://example.com/path"


def test_public_hostname_resolving_to_private_ip_rejected() -> None:
    fake = _fake_getaddrinfo({"sneaky.example.com": ["10.0.0.42"]})
    with patch("apis.shared.security.url_validator.socket.getaddrinfo", fake):
        with pytest.raises(UrlValidationError):
            validate_external_url("https://sneaky.example.com/")


def test_dns_rebinding_resistance_mixed_results_rejected() -> None:
    """If any resolved address is forbidden, the URL must be rejected."""
    fake = _fake_getaddrinfo({"rebind.example.com": ["93.184.216.34", "169.254.169.254"]})
    with patch("apis.shared.security.url_validator.socket.getaddrinfo", fake):
        with pytest.raises(UrlValidationError):
            validate_external_url("https://rebind.example.com/")


def test_dns_resolution_failure_rejected() -> None:
    fake = _fake_getaddrinfo({})  # nothing resolves
    with patch("apis.shared.security.url_validator.socket.getaddrinfo", fake):
        with pytest.raises(UrlValidationError):
            validate_external_url("https://nope.invalid/")


# ---- Schemes and inputs ---------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/",
        "file:///etc/passwd",
        "gopher://example.com/",
        "data:text/plain,hi",
        "javascript:alert(1)",
    ],
)
def test_disallowed_schemes_rejected(url: str) -> None:
    with pytest.raises(UrlValidationError):
        validate_external_url(url)


def test_empty_input_rejected() -> None:
    with pytest.raises(UrlValidationError):
        validate_external_url("")


def test_missing_host_rejected() -> None:
    with pytest.raises(UrlValidationError):
        validate_external_url("http:///path")


def test_embedded_credentials_rejected() -> None:
    fake = _fake_getaddrinfo({"example.com": ["93.184.216.34"]})
    with patch("apis.shared.security.url_validator.socket.getaddrinfo", fake):
        with pytest.raises(UrlValidationError):
            validate_external_url("https://user:pass@example.com/")


# ---- Domain allowlist -----------------------------------------------------


def test_allowlist_allows_listed_host() -> None:
    fake = _fake_getaddrinfo({"accounts.google.com": ["142.250.190.46"]})
    with patch("apis.shared.security.url_validator.socket.getaddrinfo", fake):
        url = "https://accounts.google.com/.well-known/openid-configuration"
        assert validate_external_url(url, domain_allowlist={"accounts.google.com"}) == url


def test_allowlist_rejects_unlisted_host() -> None:
    fake = _fake_getaddrinfo({"evil.example.com": ["93.184.216.34"]})
    with patch("apis.shared.security.url_validator.socket.getaddrinfo", fake):
        with pytest.raises(UrlValidationError):
            validate_external_url(
                "https://evil.example.com/",
                domain_allowlist={"accounts.google.com"},
            )


def test_allowlist_does_not_match_subdomain_implicitly() -> None:
    """Subdomains must be listed explicitly."""
    fake = _fake_getaddrinfo({"sub.example.com": ["93.184.216.34"]})
    with patch("apis.shared.security.url_validator.socket.getaddrinfo", fake):
        with pytest.raises(UrlValidationError):
            validate_external_url(
                "https://sub.example.com/",
                domain_allowlist={"example.com"},
            )


def test_allowlist_case_insensitive() -> None:
    fake = _fake_getaddrinfo({"example.com": ["93.184.216.34"]})
    with patch("apis.shared.security.url_validator.socket.getaddrinfo", fake):
        assert validate_external_url("https://EXAMPLE.com/", domain_allowlist={"Example.com"}) == "https://EXAMPLE.com/"


# ---- Custom schemes -------------------------------------------------------


def test_custom_allowed_schemes() -> None:
    fake = _fake_getaddrinfo({"example.com": ["93.184.216.34"]})
    with patch("apis.shared.security.url_validator.socket.getaddrinfo", fake):
        assert validate_external_url("https://example.com/", allow_schemes={"https"}) == "https://example.com/"
        with pytest.raises(UrlValidationError):
            validate_external_url("http://example.com/", allow_schemes={"https"})
