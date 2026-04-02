"""Extended auth_providers service + repository tests for deeper coverage."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx


class TestAuthProviderServiceExtended:
    @pytest.fixture(autouse=True)
    def _setup(self, auth_provider_repository):
        from apis.shared.auth_providers.service import AuthProviderService
        self.svc = AuthProviderService(repository=auth_provider_repository)

    def _make_create(self, pid="test-provider", **kw):
        from apis.shared.auth_providers.models import AuthProviderCreate
        defaults = dict(
            provider_id=pid, display_name="Test", provider_type="oidc",
            issuer_url="https://auth.example.com",
            authorization_endpoint="https://auth.example.com/authorize",
            token_endpoint="https://auth.example.com/token",
            jwks_uri="https://auth.example.com/.well-known/jwks.json",
            client_id="cid", client_secret="csec",
        )
        defaults.update(kw)
        return AuthProviderCreate(**defaults)

    @pytest.mark.asyncio
    async def test_create_provider_full(self):
        p = await self.svc.create_provider(self._make_create())
        assert p.provider_id == "test-provider"

    @pytest.mark.asyncio
    async def test_create_provider_invalid_id(self):
        with pytest.raises(Exception):
            await self.svc.create_provider(self._make_create(pid="INVALID!"))

    @pytest.mark.asyncio
    async def test_create_provider_invalid_regex(self):
        with pytest.raises(Exception):
            await self.svc.create_provider(self._make_create(user_id_pattern="[invalid"))

    @pytest.mark.asyncio
    async def test_create_with_auto_discovery(self):
        create = self._make_create(
            authorization_endpoint=None, token_endpoint=None, jwks_uri=None,
        )
        create.auto_discover = True
        discovery_data = {
            "issuer": "https://auth.example.com",
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
            "jwks_uri": "https://auth.example.com/.well-known/jwks.json",
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = discovery_data
        mock_resp.raise_for_status = MagicMock()

        with patch("apis.shared.auth_providers.service.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=MagicMock(
                get=AsyncMock(return_value=mock_resp)
            ))
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            p = await self.svc.create_provider(create)
            assert p.authorization_endpoint == "https://auth.example.com/authorize"

    @pytest.mark.asyncio
    async def test_update_provider(self):
        from apis.shared.auth_providers.models import AuthProviderUpdate
        await self.svc.create_provider(self._make_create())
        updated = await self.svc.update_provider("test-provider", AuthProviderUpdate(display_name="Updated"))
        assert updated.display_name == "Updated"

    @pytest.mark.asyncio
    async def test_update_provider_invalid_regex(self):
        from apis.shared.auth_providers.models import AuthProviderUpdate
        await self.svc.create_provider(self._make_create())
        with pytest.raises(Exception):
            await self.svc.update_provider("test-provider", AuthProviderUpdate(user_id_pattern="[bad"))

    @pytest.mark.asyncio
    async def test_get_provider(self):
        await self.svc.create_provider(self._make_create())
        p = await self.svc.get_provider("test-provider")
        assert p is not None

    @pytest.mark.asyncio
    async def test_list_providers(self):
        await self.svc.create_provider(self._make_create("prov-one"))
        await self.svc.create_provider(self._make_create("prov-two"))
        providers = await self.svc.list_providers()
        assert len(providers) == 2

    @pytest.mark.asyncio
    async def test_delete_provider(self):
        await self.svc.create_provider(self._make_create())
        assert await self.svc.delete_provider("test-provider") is True

    @pytest.mark.asyncio
    async def test_get_client_secret(self):
        await self.svc.create_provider(self._make_create())
        secret = await self.svc.get_client_secret("test-provider")
        assert secret == "csec"


class TestAuthProviderRepositoryExtended:
    @pytest.fixture(autouse=True)
    def _setup(self, auth_provider_repository):
        self.repo = auth_provider_repository

    def _make_create(self, pid="test-provider", **kw):
        from apis.shared.auth_providers.models import AuthProviderCreate
        defaults = dict(
            provider_id=pid, display_name="Test", provider_type="oidc",
            issuer_url="https://auth.example.com",
            authorization_endpoint="https://auth.example.com/authorize",
            token_endpoint="https://auth.example.com/token",
            jwks_uri="https://auth.example.com/.well-known/jwks.json",
            client_id="cid", client_secret="csec",
        )
        defaults.update(kw)
        return AuthProviderCreate(**defaults)

    @pytest.mark.asyncio
    async def test_create_and_get(self):
        p = await self.repo.create_provider(self._make_create())
        assert p.provider_id == "test-provider"
        got = await self.repo.get_provider("test-provider")
        assert got is not None

    @pytest.mark.asyncio
    async def test_list_enabled_only(self):
        await self.repo.create_provider(self._make_create("prov-one", enabled=True))
        await self.repo.create_provider(self._make_create("prov-two", enabled=False))
        enabled = await self.repo.list_providers(enabled_only=True)
        assert all(p.enabled for p in enabled)

    @pytest.mark.asyncio
    async def test_update_provider(self):
        from apis.shared.auth_providers.models import AuthProviderUpdate
        await self.repo.create_provider(self._make_create())
        updated = await self.repo.update_provider("test-provider", AuthProviderUpdate(display_name="New"))
        assert updated.display_name == "New"

    @pytest.mark.asyncio
    async def test_update_nonexistent(self):
        from apis.shared.auth_providers.models import AuthProviderUpdate
        assert await self.repo.update_provider("nope", AuthProviderUpdate(display_name="X")) is None

    @pytest.mark.asyncio
    async def test_delete_provider(self):
        await self.repo.create_provider(self._make_create())
        assert await self.repo.delete_provider("test-provider") is True
        assert await self.repo.get_provider("test-provider") is None

    @pytest.mark.asyncio
    async def test_update_client_secret(self):
        from apis.shared.auth_providers.models import AuthProviderUpdate
        await self.repo.create_provider(self._make_create())
        await self.repo.update_provider("test-provider", AuthProviderUpdate(client_secret="new_sec"))
        secret = await self.repo.get_client_secret("test-provider")
        assert secret == "new_sec"

    @pytest.mark.asyncio
    async def test_disabled_repo(self, monkeypatch):
        monkeypatch.delenv("DYNAMODB_AUTH_PROVIDERS_TABLE_NAME", raising=False)
        from apis.shared.auth_providers.repository import AuthProviderRepository
        repo = AuthProviderRepository(table_name=None)
        assert repo.enabled is False
        assert await repo.get_provider("x") is None
        assert await repo.list_providers() == []
        assert await repo.delete_provider("x") is False
