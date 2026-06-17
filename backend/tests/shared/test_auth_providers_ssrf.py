"""Tests for SSRF defense in OIDC discovery and auth-provider connectivity tests.

Two endpoints make outbound HTTP requests against admin-controlled URLs:

* ``POST /admin/auth-providers/discover`` constructs
  ``{issuer_url}/.well-known/openid-configuration`` and fetches it.
* ``POST /admin/auth-providers/{id}/test`` fetches every URL stored on
  the provider (jwks_uri, token_endpoint, issuer_url's discovery doc).

These tests pin the contract that:

* The discover endpoint refuses any issuer URL that fails the SSRF
  validator (loopback, link-local, RFC1918, multicast, reserved, cloud
  metadata, or a non-https scheme).
* The provider-test endpoint validates each stored URL before
  contacting it; URLs that fail the validator surface as ``False`` in
  the per-endpoint reachability map alongside a generic error string.
"""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from apis.shared.auth_providers.models import AuthProvider
from apis.shared.auth_providers.service import AuthProviderService


def _fake_getaddrinfo(mapping: dict[str, list[str]]):
    def _impl(host, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        results = []
        for ip in mapping[host]:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            results.append((family, socket.SOCK_STREAM, 0, "", (ip, 0)))
        return results

    return _impl


def _service() -> AuthProviderService:
    """Build an AuthProviderService without touching DynamoDB or
    secrets."""
    svc = AuthProviderService.__new__(AuthProviderService)
    svc._repo = MagicMock()
    svc._cognito_idp = MagicMock()
    return svc


# ---------------------------------------------------------------------------
# F13: discover_endpoints validates issuer_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/",
        "http://127.0.0.1:8000/",
        "http://10.0.0.5/",
        "https://192.168.1.1/",
        "http://[::1]/",
        # http:// is not acceptable for an OIDC issuer; force https.
        "http://accounts.google.com/",
        # data:/file:/javascript: schemes
        "file:///etc/passwd",
        "javascript:alert(1)",
    ],
)
@pytest.mark.asyncio
async def test_discover_rejects_disallowed_issuer_urls(url: str) -> None:
    svc = _service()
    with patch("apis.shared.auth_providers.service.httpx.AsyncClient") as client_cls:
        with pytest.raises(HTTPException) as excinfo:
            await svc.discover_endpoints(url)

    assert excinfo.value.status_code == 400
    # No outbound HTTP client was constructed.
    client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_discover_accepts_legitimate_https_issuer() -> None:
    svc = _service()

    fake_dns = _fake_getaddrinfo({"accounts.google.com": ["142.250.190.46"]})

    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "issuer": "https://accounts.google.com",
        "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
    }
    response.raise_for_status = MagicMock()

    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "apis.shared.security.url_validator.socket.getaddrinfo", fake_dns
    ), patch(
        "apis.shared.auth_providers.service.httpx.AsyncClient", return_value=cm
    ):
        result = await svc.discover_endpoints("https://accounts.google.com")

    assert result.issuer == "https://accounts.google.com"


# ---------------------------------------------------------------------------
# F12: test_provider validates each stored URL before contacting it
# ---------------------------------------------------------------------------


def _provider_with_urls(
    *,
    jwks_uri: str | None,
    token_endpoint: str | None,
    issuer_url: str,
) -> AuthProvider:
    """Minimal AuthProvider stub with just the URL fields the test
    helper reads."""
    return AuthProvider(
        provider_id="test-id",
        display_name="Test",
        provider_type="oidc",
        enabled=True,
        issuer_url=issuer_url,
        client_id="cid",
        jwks_uri=jwks_uri,
        token_endpoint=token_endpoint,
    )


@pytest.mark.asyncio
async def test_test_provider_skips_disallowed_jwks_uri() -> None:
    svc = _service()
    provider = _provider_with_urls(
        jwks_uri="http://169.254.169.254/jwks",
        token_endpoint="https://accounts.google.com/o/oauth2/token",
        issuer_url="https://accounts.google.com",
    )
    svc._repo.get_provider = AsyncMock(return_value=provider)

    fake_dns = _fake_getaddrinfo(
        {"accounts.google.com": ["142.250.190.46"]}
    )

    # Build a context-manager mock for httpx.AsyncClient; record GET targets.
    targets: list[str] = []

    async def fake_get(url, *args, **kwargs):
        targets.append(url)
        resp = MagicMock()
        resp.status_code = 200
        return resp

    async def fake_options(url, *args, **kwargs):
        targets.append(url)
        resp = MagicMock()
        resp.status_code = 204
        return resp

    client = MagicMock()
    client.get = AsyncMock(side_effect=fake_get)
    client.options = AsyncMock(side_effect=fake_options)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "apis.shared.security.url_validator.socket.getaddrinfo", fake_dns
    ), patch(
        "apis.shared.auth_providers.service.httpx.AsyncClient", return_value=cm
    ):
        result = await svc.test_provider("test-id")

    # The 169.254 jwks URI must never have been contacted.
    assert "http://169.254.169.254/jwks" not in targets
    # And the result reflects that fact.
    assert result["jwks_reachable"] is False


@pytest.mark.asyncio
async def test_test_provider_skips_disallowed_token_endpoint() -> None:
    svc = _service()
    provider = _provider_with_urls(
        jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
        token_endpoint="http://10.0.0.5/token",
        issuer_url="https://accounts.google.com",
    )
    svc._repo.get_provider = AsyncMock(return_value=provider)

    fake_dns = _fake_getaddrinfo(
        {
            "accounts.google.com": ["142.250.190.46"],
            "www.googleapis.com": ["142.250.190.74"],
        }
    )

    targets: list[str] = []

    async def fake_get(url, *args, **kwargs):
        targets.append(url)
        resp = MagicMock()
        resp.status_code = 200
        return resp

    async def fake_options(url, *args, **kwargs):
        targets.append(url)
        resp = MagicMock()
        resp.status_code = 204
        return resp

    client = MagicMock()
    client.get = AsyncMock(side_effect=fake_get)
    client.options = AsyncMock(side_effect=fake_options)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "apis.shared.security.url_validator.socket.getaddrinfo", fake_dns
    ), patch(
        "apis.shared.auth_providers.service.httpx.AsyncClient", return_value=cm
    ):
        result = await svc.test_provider("test-id")

    assert "http://10.0.0.5/token" not in targets
    assert result["token_endpoint_reachable"] is False


@pytest.mark.asyncio
async def test_test_provider_skips_disallowed_issuer() -> None:
    svc = _service()
    provider = _provider_with_urls(
        jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
        token_endpoint="https://accounts.google.com/o/oauth2/token",
        issuer_url="http://[fd00:ec2::254]",
    )
    svc._repo.get_provider = AsyncMock(return_value=provider)

    fake_dns = _fake_getaddrinfo(
        {
            "accounts.google.com": ["142.250.190.46"],
            "www.googleapis.com": ["142.250.190.74"],
        }
    )

    targets: list[str] = []

    async def fake_get(url, *args, **kwargs):
        targets.append(url)
        resp = MagicMock()
        resp.status_code = 200
        return resp

    async def fake_options(url, *args, **kwargs):
        targets.append(url)
        resp = MagicMock()
        resp.status_code = 204
        return resp

    client = MagicMock()
    client.get = AsyncMock(side_effect=fake_get)
    client.options = AsyncMock(side_effect=fake_options)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "apis.shared.security.url_validator.socket.getaddrinfo", fake_dns
    ), patch(
        "apis.shared.auth_providers.service.httpx.AsyncClient", return_value=cm
    ):
        result = await svc.test_provider("test-id")

    assert all("fd00" not in t for t in targets)
    assert result["discovery_reachable"] is False
