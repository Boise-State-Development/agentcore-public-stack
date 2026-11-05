"""OAuth provider repository tests (moto DynamoDB)."""

import pytest

from apis.shared.oauth.models import (
    OAuthProvider,
    OAuthProviderType,
    OAuthProviderUpdate,
)
from apis.shared.oauth.provider_repository import OAuthProviderRepository


def _make_provider(provider_id="github", **kw) -> OAuthProvider:
    defaults = dict(
        provider_id=provider_id,
        display_name="GitHub",
        provider_type=OAuthProviderType.GITHUB,
        scopes=["repo"],
        allowed_roles=["editor"],
        credential_provider_arn=f"arn:aws:bedrock-agentcore:us-east-1:1:cp/{provider_id}",
        callback_url=f"https://bedrock-agentcore.us-east-1.amazonaws.com/cb/{provider_id}",
    )
    defaults.update(kw)
    return OAuthProvider(**defaults)


class TestOAuthProviderRepository:
    @pytest.mark.asyncio
    async def test_put_and_get(self, oauth_provider_repository):
        await oauth_provider_repository.put_provider(_make_provider())
        result = await oauth_provider_repository.get_provider("github")
        assert result is not None
        assert result.display_name == "GitHub"
        assert result.callback_url.endswith("/cb/github")
        assert result.credential_provider_arn

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, oauth_provider_repository):
        assert await oauth_provider_repository.get_provider("nope") is None

    @pytest.mark.asyncio
    async def test_list_all(self, oauth_provider_repository):
        await oauth_provider_repository.put_provider(_make_provider("p1"))
        await oauth_provider_repository.put_provider(_make_provider("p2"))
        providers = await oauth_provider_repository.list_providers()
        assert len(providers) == 2

    @pytest.mark.asyncio
    async def test_list_enabled_only(self, oauth_provider_repository):
        await oauth_provider_repository.put_provider(_make_provider("p1"))
        await oauth_provider_repository.put_provider(_make_provider("p2", enabled=False))
        providers = await oauth_provider_repository.list_providers(enabled_only=True)
        assert len(providers) == 1
        assert providers[0].provider_id == "p1"

    @pytest.mark.asyncio
    async def test_apply_metadata_update(self, oauth_provider_repository):
        await oauth_provider_repository.put_provider(_make_provider())
        updated = await oauth_provider_repository.apply_metadata_update(
            "github",
            OAuthProviderUpdate(display_name="GH", scopes=["repo", "read:user"]),
        )
        assert updated.display_name == "GH"
        assert updated.scopes == ["repo", "read:user"]

    @pytest.mark.asyncio
    async def test_apply_metadata_update_nonexistent(self, oauth_provider_repository):
        updates = OAuthProviderUpdate(display_name="X")
        assert await oauth_provider_repository.apply_metadata_update("nope", updates) is None

    @pytest.mark.asyncio
    async def test_delete_provider(self, oauth_provider_repository):
        await oauth_provider_repository.put_provider(_make_provider())
        assert await oauth_provider_repository.delete_provider("github") is True
        assert await oauth_provider_repository.get_provider("github") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, oauth_provider_repository):
        assert await oauth_provider_repository.delete_provider("nope") is False

    def test_disabled_when_no_table(self):
        repo = OAuthProviderRepository(table_name=None)
        assert repo.enabled is False

    @pytest.mark.asyncio
    async def test_disabled_repo_is_inert(self):
        repo = OAuthProviderRepository(table_name=None)
        assert await repo.get_provider("x") is None
        assert await repo.list_providers() == []
        assert await repo.delete_provider("x") is False


class TestCrossTaskStaleness:
    """The config cache is per ECS task; admin reads must not depend on it."""

    @pytest.mark.asyncio
    async def test_write_on_one_task_visible_to_admin_read_on_another(
        self, oauth_provider_repository
    ):
        from apis.shared.caching import config_cache
        from apis.shared.caching.config_cache import ConfigListCache

        repo = oauth_provider_repository
        saved = config_cache._cache
        try:
            # Task B warms its own cache with the pre-write list.
            task_b = ConfigListCache()
            config_cache._cache = task_b
            await repo.put_provider(_make_provider("p1", display_name="Old"))
            assert [p.display_name for p in await repo.list_providers()] == ["Old"]

            # Task A handles the write; its invalidation never reaches task B.
            config_cache._cache = ConfigListCache()
            await repo.put_provider(_make_provider("p1", display_name="New"))
            await repo.put_provider(_make_provider("p2", display_name="Other"))

            config_cache._cache = task_b
            stale = await repo.list_providers()
            assert [p.display_name for p in stale] == ["Old"]  # the bug, cached path
            fresh = await repo.list_providers(fresh=True)
            assert [p.display_name for p in fresh] == ["New", "Other"]
            fresh_enabled = await repo.list_providers(enabled_only=True, fresh=True)
            assert len(fresh_enabled) == 2
        finally:
            config_cache._cache = saved
