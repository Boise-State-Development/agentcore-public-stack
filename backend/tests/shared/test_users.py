"""Task 3: Users repository (moto DynamoDB) + sync service tests."""

import pytest
from apis.shared.users.models import UserProfile, UserStatus


def _make_profile(user_id="u1", email="alice@example.com", **kw):
    defaults = dict(
        user_id=user_id, email=email, name="Alice",
        email_domain="example.com", created_at="2026-01-01T00:00:00Z",
        last_login_at="2026-01-01T00:00:00Z", status=UserStatus.ACTIVE,
    )
    defaults.update(kw)
    return UserProfile(**defaults)


# ===================================================================
# UserRepository
# ===================================================================

class TestUserRepositoryEnabled:
    def test_enabled(self, user_repository):
        assert user_repository.enabled is True

    def test_disabled_when_no_table(self):
        from apis.shared.users.repository import UserRepository
        repo = UserRepository(table_name="")
        assert repo.enabled is False

    @pytest.mark.asyncio
    async def test_create_and_get(self, user_repository):
        p = _make_profile()
        await user_repository.create_user(p)
        result = await user_repository.get_user("u1")
        assert result is not None
        assert result.email == "alice@example.com"

    @pytest.mark.asyncio
    async def test_create_duplicate_raises(self, user_repository):
        p = _make_profile()
        await user_repository.create_user(p)
        with pytest.raises(ValueError, match="already exists"):
            await user_repository.create_user(p)

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, user_repository):
        assert await user_repository.get_user("nope") is None

    @pytest.mark.asyncio
    async def test_get_by_user_id_gsi(self, user_repository):
        await user_repository.create_user(_make_profile())
        result = await user_repository.get_user_by_user_id("u1")
        assert result is not None
        assert result.user_id == "u1"

    @pytest.mark.asyncio
    async def test_get_by_email_gsi(self, user_repository):
        await user_repository.create_user(_make_profile())
        result = await user_repository.get_user_by_email("Alice@Example.com")
        assert result is not None
        assert result.email == "alice@example.com"

    @pytest.mark.asyncio
    async def test_update_user(self, user_repository):
        p = _make_profile()
        await user_repository.create_user(p)
        p.name = "Alice Updated"
        await user_repository.update_user(p)
        result = await user_repository.get_user("u1")
        assert result.name == "Alice Updated"

    @pytest.mark.asyncio
    async def test_upsert_creates_new(self, user_repository):
        p = _make_profile()
        result, is_new = await user_repository.upsert_user(p)
        assert is_new is True
        assert await user_repository.get_user("u1") is not None

    @pytest.mark.asyncio
    async def test_upsert_updates_existing(self, user_repository):
        p = _make_profile()
        await user_repository.create_user(p)
        p.name = "Updated"
        result, is_new = await user_repository.upsert_user(p)
        assert is_new is False
        assert result.name == "Updated"

    @pytest.mark.asyncio
    async def test_list_by_domain(self, user_repository):
        await user_repository.create_user(_make_profile("u1", "a@example.com"))
        await user_repository.create_user(_make_profile("u2", "b@example.com"))
        await user_repository.create_user(_make_profile("u3", "c@other.com", email_domain="other.com"))
        items, _ = await user_repository.list_users_by_domain("example.com")
        assert len(items) == 2

    @pytest.mark.asyncio
    async def test_list_by_status(self, user_repository):
        await user_repository.create_user(_make_profile("u1"))
        await user_repository.create_user(_make_profile("u2", status=UserStatus.SUSPENDED))
        items, _ = await user_repository.list_users_by_status("active")
        assert len(items) == 1
        assert items[0].user_id == "u1"


class TestUserRepositoryDisabled:
    @pytest.fixture()
    def disabled_repo(self):
        from apis.shared.users.repository import UserRepository
        return UserRepository(table_name="")

    @pytest.mark.asyncio
    async def test_get_returns_none(self, disabled_repo):
        assert await disabled_repo.get_user("u1") is None

    @pytest.mark.asyncio
    async def test_create_raises(self, disabled_repo):
        with pytest.raises(RuntimeError):
            await disabled_repo.create_user(_make_profile())

    @pytest.mark.asyncio
    async def test_upsert_returns_existing(self, disabled_repo):
        p = _make_profile()
        result, is_new = await disabled_repo.upsert_user(p)
        assert is_new is False

    @pytest.mark.asyncio
    async def test_list_by_domain_empty(self, disabled_repo):
        items, _ = await disabled_repo.list_users_by_domain("x.com")
        assert items == []

    @pytest.mark.asyncio
    async def test_list_by_status_empty(self, disabled_repo):
        items, _ = await disabled_repo.list_users_by_status("active")
        assert items == []


# ===================================================================
# UserSyncService
# ===================================================================

class TestUserSyncService:
    @pytest.fixture()
    def sync_service(self, user_repository):
        from apis.shared.users.sync import UserSyncService
        return UserSyncService(user_repository)

    @pytest.mark.asyncio
    async def test_sync_new_user(self, sync_service):
        claims = {"sub": "u1", "email": "alice@example.com", "name": "Alice"}
        profile, is_new = await sync_service.sync_from_jwt(claims)
        assert is_new is True
        assert profile.email == "alice@example.com"

    @pytest.mark.asyncio
    async def test_sync_existing_user(self, sync_service, user_repository):
        await user_repository.create_user(_make_profile())
        claims = {"sub": "u1", "email": "alice@example.com", "name": "Alice v2"}
        profile, is_new = await sync_service.sync_from_jwt(claims)
        assert is_new is False
        assert profile.name == "Alice v2"

    @pytest.mark.asyncio
    async def test_sync_disabled(self):
        from apis.shared.users.repository import UserRepository
        from apis.shared.users.sync import UserSyncService
        repo = UserRepository(table_name="")
        svc = UserSyncService(repo)
        profile, is_new = await svc.sync_from_jwt({"sub": "u1", "email": "a@b.com"})
        assert profile is None
        assert is_new is False

    @pytest.mark.asyncio
    async def test_sync_missing_sub(self, sync_service):
        profile, is_new = await sync_service.sync_from_jwt({"email": "a@b.com"})
        assert profile is None

    @pytest.mark.asyncio
    async def test_sync_missing_email(self, sync_service):
        profile, is_new = await sync_service.sync_from_jwt({"sub": "u1"})
        assert profile is None

    @pytest.mark.asyncio
    async def test_sync_from_user_convenience(self, sync_service):
        profile, is_new = await sync_service.sync_from_user(
            user_id="u1", email="a@b.com", name="A"
        )
        assert is_new is True

    @pytest.mark.asyncio
    async def test_sync_user_from_jwt_convenience(self, sync_service):
        from unittest.mock import MagicMock
        user = MagicMock()
        user.user_id = "u1"
        user.email = "a@b.com"
        user.name = "A"
        user.roles = []
        user.picture = None
        profile, is_new = await sync_service.sync_user_from_jwt(user)
        assert is_new is True

    @pytest.mark.asyncio
    async def test_sync_timestamps_are_strict_iso8601(self, sync_service):
        """Regression: timestamps must carry a single trailing ``Z`` and no
        ``+00:00`` offset. The old ``isoformat() + "Z"`` produced ``…+00:00Z``,
        which is invalid ISO 8601 and parses to ``Invalid Date`` in Safari,
        blanking the admin user list / detail dates."""
        from datetime import datetime

        profile, _ = await sync_service.sync_from_jwt(
            {"sub": "u1", "email": "alice@example.com", "name": "Alice"}
        )
        for ts in (profile.created_at, profile.last_login_at):
            assert ts.endswith("Z")
            assert "+00:00" not in ts
            # Parses cleanly as a UTC-aware datetime (JS ``new Date`` parity).
            assert datetime.fromisoformat(ts.replace("Z", "+00:00")).tzinfo is not None


# ===================================================================
# Legacy timestamp healing on read
# ===================================================================

class TestTimestampHealingOnRead:
    """Rows persisted before the sync fix carry ``…+00:00Z``; the read path
    normalizes them to a single trailing ``Z`` so already-persisted
    ``created_at`` (never rewritten) still renders in strict engines."""

    def test_item_to_profile_heals_legacy_suffix(self, user_repository):
        item = {
            "userId": "u1",
            "email": "alice@example.com",
            "name": "Alice",
            "emailDomain": "example.com",
            "createdAt": "2026-01-01T00:00:00+00:00Z",
            "lastLoginAt": "2026-02-02T00:00:00+00:00Z",
            "status": "active",
        }
        profile = user_repository._item_to_profile(item)
        assert profile.created_at == "2026-01-01T00:00:00Z"
        assert profile.last_login_at == "2026-02-02T00:00:00Z"

    def test_item_to_profile_leaves_valid_suffix_untouched(self, user_repository):
        item = {
            "userId": "u1",
            "email": "alice@example.com",
            "emailDomain": "example.com",
            "createdAt": "2026-01-01T00:00:00Z",
            "lastLoginAt": "2026-02-02T00:00:00Z",
            "status": "active",
        }
        profile = user_repository._item_to_profile(item)
        assert profile.created_at == "2026-01-01T00:00:00Z"
        assert profile.last_login_at == "2026-02-02T00:00:00Z"

    def test_item_to_list_item_heals_legacy_suffix(self, user_repository):
        item = {
            "userId": "u1",
            "email": "alice@example.com",
            "status": "active",
            "lastLoginAt": "2026-02-02T00:00:00+00:00Z",
        }
        list_item = user_repository._item_to_list_item(item)
        assert list_item.last_login_at == "2026-02-02T00:00:00Z"
