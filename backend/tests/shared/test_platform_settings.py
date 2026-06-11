"""Unit tests for platform-wide chat-mode settings (models, repository, service)."""

import pytest
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
from pydantic import ValidationError

from apis.shared.platform_settings.models import (
    DEFAULT_CHAT_MODE,
    ChatModeSettings,
    ChatModeSettingsUpdate,
)
from apis.shared.platform_settings.repository import (
    CHAT_MODE_PK,
    CHAT_MODE_SK,
    PlatformSettingsRepository,
)
from apis.shared.platform_settings.service import ChatModeSettingsService

pytestmark = pytest.mark.asyncio


# =========================================================================
# Model tests
# =========================================================================


class TestChatModeSettings:
    def test_defaults_reproduce_pre_settings_behavior(self):
        """Defaults must match the hardcoded server behavior (skill default, toggling allowed)."""
        settings = ChatModeSettings()
        assert settings.default_mode == DEFAULT_CHAT_MODE == "skill"
        assert settings.allow_mode_toggle is True
        assert settings.updated_at is None
        assert settings.updated_by is None

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValidationError):
            ChatModeSettings(default_mode="voice")

    def test_dynamo_round_trip(self):
        from datetime import datetime, timezone

        original = ChatModeSettings(
            default_mode="chat",
            allow_mode_toggle=False,
            updated_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
            updated_by="admin@example.com",
        )
        restored = ChatModeSettings.from_dynamo_item(original.to_dynamo_item())
        assert restored == original

    def test_from_dynamo_item_tolerates_missing_fields(self):
        restored = ChatModeSettings.from_dynamo_item({})
        assert restored.default_mode == "skill"
        assert restored.allow_mode_toggle is True


class TestChatModeSettingsUpdate:
    def test_accepts_camel_case_aliases(self):
        update = ChatModeSettingsUpdate.model_validate(
            {"defaultMode": "chat", "allowModeToggle": False}
        )
        assert update.default_mode == "chat"
        assert update.allow_mode_toggle is False

    def test_accepts_snake_case_field_names(self):
        update = ChatModeSettingsUpdate(default_mode="skill", allow_mode_toggle=True)
        assert update.default_mode == "skill"

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValidationError):
            ChatModeSettingsUpdate.model_validate(
                {"defaultMode": "agent", "allowModeToggle": True}
            )


# =========================================================================
# Repository tests
# =========================================================================


class TestPlatformSettingsRepository:
    def _make_repo(self) -> PlatformSettingsRepository:
        """Create a repository with a mocked DynamoDB table."""
        with patch("apis.shared.platform_settings.repository.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_resource = MagicMock()
            mock_resource.Table.return_value = mock_table
            mock_boto.Session.return_value.resource.return_value = mock_resource
            mock_boto.resource.return_value = mock_resource

            repo = PlatformSettingsRepository(table_name="test-table")
            repo._table = mock_table
            return repo

    async def test_get_returns_none_when_item_missing(self):
        repo = self._make_repo()
        repo._table.get_item.return_value = {}

        result = await repo.get_chat_mode_settings()
        assert result is None

        repo._table.get_item.assert_called_once_with(
            Key={"PK": CHAT_MODE_PK, "SK": CHAT_MODE_SK}
        )

    async def test_get_parses_stored_item(self):
        repo = self._make_repo()
        repo._table.get_item.return_value = {
            "Item": {
                "PK": CHAT_MODE_PK,
                "SK": CHAT_MODE_SK,
                "defaultMode": "chat",
                "allowModeToggle": False,
                "updatedBy": "admin@example.com",
            }
        }

        result = await repo.get_chat_mode_settings()
        assert result is not None
        assert result.default_mode == "chat"
        assert result.allow_mode_toggle is False
        assert result.updated_by == "admin@example.com"

    async def test_get_raises_on_client_error(self):
        repo = self._make_repo()
        repo._table.get_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "boom"}}, "GetItem"
        )
        with pytest.raises(ClientError):
            await repo.get_chat_mode_settings()

    async def test_put_writes_sentinel_item(self):
        repo = self._make_repo()
        await repo.put_chat_mode_settings(
            ChatModeSettings(default_mode="chat", allow_mode_toggle=False)
        )

        item = repo._table.put_item.call_args.kwargs["Item"]
        assert item["PK"] == CHAT_MODE_PK
        assert item["SK"] == CHAT_MODE_SK
        assert item["defaultMode"] == "chat"
        assert item["allowModeToggle"] is False

    async def test_disabled_without_table_name(self):
        with patch.dict("os.environ", {}, clear=False):
            with patch("os.getenv", side_effect=lambda k, d=None: d):
                repo = PlatformSettingsRepository()
        assert repo.enabled is False
        assert await repo.get_chat_mode_settings() is None
        with pytest.raises(RuntimeError):
            await repo.put_chat_mode_settings(ChatModeSettings())


# =========================================================================
# Service tests (TTL cache + degradation)
# =========================================================================


class _FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TestChatModeSettingsService:
    async def test_returns_defaults_when_nothing_stored(self):
        service = ChatModeSettingsService(
            repository=_AsyncRepo(None), clock=_FakeClock()
        )

        settings = await service.get_settings()
        assert settings.default_mode == "skill"
        assert settings.allow_mode_toggle is True

    async def test_caches_within_ttl(self):
        repo = _AsyncRepo(ChatModeSettings(default_mode="chat"))
        clock = _FakeClock()
        service = ChatModeSettingsService(
            repository=repo, cache_ttl_seconds=60.0, clock=clock
        )

        first = await service.get_settings()
        clock.now += 30.0
        second = await service.get_settings()

        assert first.default_mode == second.default_mode == "chat"
        assert repo.get_calls == 1

    async def test_refetches_after_ttl_expiry(self):
        repo = _AsyncRepo(ChatModeSettings(default_mode="chat"))
        clock = _FakeClock()
        service = ChatModeSettingsService(
            repository=repo, cache_ttl_seconds=60.0, clock=clock
        )

        await service.get_settings()
        repo.stored = ChatModeSettings(default_mode="skill")
        clock.now += 61.0

        refreshed = await service.get_settings()
        assert refreshed.default_mode == "skill"
        assert repo.get_calls == 2

    async def test_returns_defaults_on_read_error(self):
        repo = _AsyncRepo(None, raise_on_get=True)
        service = ChatModeSettingsService(repository=repo, clock=_FakeClock())

        settings = await service.get_settings()
        assert settings.default_mode == "skill"
        assert settings.allow_mode_toggle is True

    async def test_update_writes_through_and_busts_cache(self):
        repo = _AsyncRepo(ChatModeSettings(default_mode="skill"))
        clock = _FakeClock()
        service = ChatModeSettingsService(
            repository=repo, cache_ttl_seconds=60.0, clock=clock
        )

        await service.get_settings()  # prime the cache
        updated = await service.update_settings(
            ChatModeSettingsUpdate(default_mode="chat", allow_mode_toggle=False),
            updated_by="admin@example.com",
        )

        assert repo.stored.default_mode == "chat"
        assert updated.updated_by == "admin@example.com"
        assert updated.updated_at is not None

        # Cached value reflects the update without another read
        current = await service.get_settings()
        assert current.default_mode == "chat"
        assert repo.get_calls == 1


class _AsyncRepo:
    """Duck-typed in-memory stand-in for PlatformSettingsRepository."""

    def __init__(self, stored, raise_on_get: bool = False):
        self.stored = stored
        self.raise_on_get = raise_on_get
        self.get_calls = 0

    @property
    def enabled(self) -> bool:
        return True

    async def get_chat_mode_settings(self):
        self.get_calls += 1
        if self.raise_on_get:
            raise RuntimeError("read failed")
        return self.stored

    async def put_chat_mode_settings(self, settings) -> None:
        self.stored = settings
