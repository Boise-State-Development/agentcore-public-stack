"""Task 4: Auth providers repository (moto DynamoDB + Secrets Manager) + service tests."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from apis.shared.auth_providers.models import AuthProvider, AuthProviderCreate, AuthProviderUpdate


def _make_create(**kw):
    defaults = dict(
        provider_id="okta-1", display_name="Okta", provider_type="oidc",
        issuer_url="https://okta.example.com", client_id="cid", client_secret="secret",
        enabled=True, authorization_endpoint="https://okta.example.com/authorize",
        token_endpoint="https://okta.example.com/token",
        jwks_uri="https://okta.example.com/keys",
    )
    defaults.update(kw)
    return AuthProviderCreate(**defaults)


# ===================================================================
# AuthProviderRepository
# ===================================================================

class TestAuthProviderRepository:
    @pytest.mark.asyncio
    async def test_create_and_get(self, auth_provider_repository):
        data = _make_create()
        provider = await auth_provider_repository.create_provider(data)
        assert provider.provider_id == "okta-1"
        result = await auth_provider_repository.get_provider("okta-1")
        assert result is not None
        assert result.display_name == "Okta"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, auth_provider_repository):
        assert await auth_provider_repository.get_provider("nope") is None

    @pytest.mark.asyncio
    async def test_list_all(self, auth_provider_repository):
        await auth_provider_repository.create_provider(_make_create(provider_id="p1"))
        await auth_provider_repository.create_provider(_make_create(provider_id="p2"))
        providers = await auth_provider_repository.list_providers()
        assert len(providers) == 2

    @pytest.mark.asyncio
    async def test_list_enabled_only(self, auth_provider_repository):
        await auth_provider_repository.create_provider(_make_create(provider_id="p1", enabled=True))
        await auth_provider_repository.create_provider(_make_create(provider_id="p2", enabled=False))
        providers = await auth_provider_repository.list_providers(enabled_only=True)
        assert len(providers) == 1
        assert providers[0].provider_id == "p1"

    @pytest.mark.asyncio
    async def test_update_provider(self, auth_provider_repository):
        await auth_provider_repository.create_provider(_make_create())
        updates = AuthProviderUpdate(display_name="Okta v2")
        result = await auth_provider_repository.update_provider("okta-1", updates)
        assert result is not None
        assert result.display_name == "Okta v2"

    @pytest.mark.asyncio
    async def test_delete_provider(self, auth_provider_repository):
        await auth_provider_repository.create_provider(_make_create())
        deleted = await auth_provider_repository.delete_provider("okta-1")
        assert deleted is True
        assert await auth_provider_repository.get_provider("okta-1") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, auth_provider_repository):
        deleted = await auth_provider_repository.delete_provider("nope")
        assert deleted is False

    @pytest.mark.asyncio
    async def test_client_secret_stored_and_retrieved(self, auth_provider_repository):
        await auth_provider_repository.create_provider(_make_create())
        secret = await auth_provider_repository.get_client_secret("okta-1")
        assert secret == "secret"

    @pytest.mark.asyncio
    async def test_client_secret_missing_provider(self, auth_provider_repository):
        secret = await auth_provider_repository.get_client_secret("nope")
        assert secret is None

    def test_disabled_when_no_table(self):
        from apis.shared.auth_providers.repository import AuthProviderRepository
        repo = AuthProviderRepository(table_name=None)
        assert repo.enabled is False


# ===================================================================
# AuthProviderService
# ===================================================================

class TestAuthProviderService:
    @pytest.fixture()
    def service(self, auth_provider_repository):
        from apis.shared.auth_providers.service import AuthProviderService
        return AuthProviderService(auth_provider_repository)

    @pytest.mark.asyncio
    async def test_create_with_all_endpoints(self, service):
        data = _make_create()
        provider = await service.create_provider(data)
        assert provider.provider_id == "okta-1"

    def test_create_invalid_id_rejected_by_model(self, service):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            _make_create(provider_id="INVALID ID!")

    @pytest.mark.asyncio
    async def test_discover_endpoints_success(self, service):
        discovery_data = {
            "issuer": "https://okta.example.com",
            "authorization_endpoint": "https://okta.example.com/authorize",
            "token_endpoint": "https://okta.example.com/token",
            "jwks_uri": "https://okta.example.com/keys",
        }
        mock_response = MagicMock()
        mock_response.json.return_value = discovery_data
        mock_response.raise_for_status = MagicMock()

        # The URL validator resolves DNS — fake the lookup so the test
        # hostname can pass without an internet round-trip.
        import socket
        def _fake_getaddrinfo(host, *a, **kw):
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]

        with patch(
            "apis.shared.security.url_validator.socket.getaddrinfo", _fake_getaddrinfo
        ), patch("apis.shared.auth_providers.service.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=MagicMock(get=AsyncMock(return_value=mock_response)))
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await service.discover_endpoints("https://okta.example.com")
            assert result.authorization_endpoint == "https://okta.example.com/authorize"

    @pytest.mark.asyncio
    async def test_discover_endpoints_http_error(self, service):
        import httpx
        from fastapi import HTTPException
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError("not found", request=MagicMock(), response=mock_response)

        import socket
        def _fake_getaddrinfo(host, *a, **kw):
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]

        with patch(
            "apis.shared.security.url_validator.socket.getaddrinfo", _fake_getaddrinfo
        ), patch("apis.shared.auth_providers.service.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=MagicMock(get=AsyncMock(return_value=mock_response)))
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(HTTPException):
                await service.discover_endpoints("https://bad.example.com")

    @pytest.mark.asyncio
    async def test_list_and_delete(self, service):
        await service.create_provider(_make_create())
        providers = await service.list_providers()
        assert len(providers) == 1
        await service.delete_provider("okta-1")
        assert len(await service.list_providers()) == 0

    @pytest.mark.asyncio
    async def test_create_invalid_regex_raises(self, service):
        from fastapi import HTTPException
        data = _make_create(user_id_pattern="[invalid")
        with pytest.raises(HTTPException, match="regex"):
            await service.create_provider(data)
