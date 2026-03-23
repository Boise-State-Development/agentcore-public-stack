"""Unit tests for system settings models and repository."""

import pytest
from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError

from apis.app_api.system.models import (
    FirstBootRequest,
    FirstBootResponse,
    SystemStatusResponse,
)
from apis.app_api.system.repository import (
    FIRST_BOOT_PK,
    FIRST_BOOT_SK,
    SystemSettingsRepository,
)

pytestmark = pytest.mark.asyncio


# =========================================================================
# Model validation tests
# =========================================================================


class TestFirstBootRequest:
    """Validates: Requirement 12.1 — first-boot request validation."""

    def test_valid_request(self):
        req = FirstBootRequest(
            username="admin", email="admin@example.com", password="Str0ng!Pass"
        )
        assert req.username == "admin"
        assert req.email == "admin@example.com"
        assert req.password == "Str0ng!Pass"

    def test_username_too_short(self):
        with pytest.raises(Exception):
            FirstBootRequest(username="ab", email="a@b.com", password="12345678")

    def test_username_too_long(self):
        with pytest.raises(Exception):
            FirstBootRequest(
                username="x" * 129, email="a@b.com", password="12345678"
            )

    def test_invalid_email(self):
        with pytest.raises(Exception):
            FirstBootRequest(
                username="admin", email="not-an-email", password="12345678"
            )

    def test_password_too_short(self):
        with pytest.raises(Exception):
            FirstBootRequest(
                username="admin", email="a@b.com", password="short"
            )


class TestFirstBootResponse:
    def test_response_fields(self):
        resp = FirstBootResponse(
            success=True, user_id="uid-123", message="done"
        )
        assert resp.success is True
        assert resp.user_id == "uid-123"


class TestSystemStatusResponse:
    def test_status_fields(self):
        status = SystemStatusResponse(first_boot_completed=False)
        assert status.first_boot_completed is False


# =========================================================================
# Repository tests
# =========================================================================


class TestSystemSettingsRepository:
    """Validates: Requirements 12.1, 12.4, 12.5."""

    def _make_repo(self) -> SystemSettingsRepository:
        """Create a repository with a mocked DynamoDB table."""
        with patch.dict(
            "os.environ",
            {"DYNAMODB_AUTH_PROVIDERS_TABLE_NAME": "test-table"},
            clear=False,
        ), patch("apis.app_api.system.repository.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_resource = MagicMock()
            mock_resource.Table.return_value = mock_table
            mock_boto.Session.return_value.resource.return_value = mock_resource
            mock_boto.resource.return_value = mock_resource

            repo = SystemSettingsRepository(table_name="test-table")
            repo._table = mock_table
            return repo

    @pytest.mark.asyncio
    async def test_get_first_boot_status_not_found(self):
        """Requirement 12.5: missing item means not bootstrapped."""
        repo = self._make_repo()
        repo._table.get_item.return_value = {}

        result = await repo.get_first_boot_status()
        assert result is None

        repo._table.get_item.assert_called_once_with(
            Key={"PK": FIRST_BOOT_PK, "SK": FIRST_BOOT_SK}
        )

    @pytest.mark.asyncio
    async def test_get_first_boot_status_found(self):
        repo = self._make_repo()
        repo._table.get_item.return_value = {
            "Item": {
                "PK": FIRST_BOOT_PK,
                "SK": FIRST_BOOT_SK,
                "completed": True,
                "completedBy": "uid-1",
            }
        }

        result = await repo.get_first_boot_status()
        assert result is not None
        assert result["completed"] is True

    @pytest.mark.asyncio
    async def test_mark_first_boot_completed_success(self):
        """Requirement 12.1: stores first-boot item in DynamoDB."""
        repo = self._make_repo()

        await repo.mark_first_boot_completed(
            user_id="uid-1", username="admin", email="admin@example.com"
        )

        repo._table.put_item.assert_called_once()
        call_kwargs = repo._table.put_item.call_args[1]
        item = call_kwargs["Item"]
        assert item["PK"] == FIRST_BOOT_PK
        assert item["SK"] == FIRST_BOOT_SK
        assert item["completed"] is True
        assert item["completedBy"] == "uid-1"
        assert item["adminUsername"] == "admin"
        assert item["adminEmail"] == "admin@example.com"
        assert "completedAt" in item
        assert call_kwargs["ConditionExpression"] == "attribute_not_exists(PK)"

    @pytest.mark.asyncio
    async def test_mark_first_boot_completed_race_condition(self):
        """Requirement 12.4: conditional write rejects duplicate."""
        repo = self._make_repo()
        repo._table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}},
            "PutItem",
        )

        with pytest.raises(ClientError) as exc_info:
            await repo.mark_first_boot_completed(
                user_id="uid-2", username="hacker", email="h@x.com"
            )
        assert (
            exc_info.value.response["Error"]["Code"]
            == "ConditionalCheckFailedException"
        )

    @pytest.mark.asyncio
    async def test_disabled_repo_returns_none(self):
        """When table name is not set, repository is disabled."""
        with patch.dict("os.environ", {}, clear=True):
            repo = SystemSettingsRepository(table_name=None)
        assert repo.enabled is False
        assert await repo.get_first_boot_status() is None

    @pytest.mark.asyncio
    async def test_disabled_repo_raises_on_write(self):
        """Disabled repository raises RuntimeError on write."""
        with patch.dict("os.environ", {}, clear=True):
            repo = SystemSettingsRepository(table_name=None)
        with pytest.raises(RuntimeError):
            await repo.mark_first_boot_completed("u", "n", "e")


# =========================================================================
# Route tests
# =========================================================================


class TestGetSystemStatusRoute:
    """Validates: Requirements 12.2, 12.3, 12.5."""

    @pytest.mark.asyncio
    async def test_status_returns_false_when_no_item(self):
        """Requirement 12.5: missing item means not bootstrapped."""
        from apis.app_api.system.routes import get_system_status

        with patch(
            "apis.app_api.system.routes.get_system_settings_repository"
        ) as mock_get_repo:
            mock_repo = MagicMock()
            mock_repo.get_first_boot_status = MagicMock(return_value=None)
            # Make the coroutine return None
            import asyncio

            future = asyncio.Future()
            future.set_result(None)
            mock_repo.get_first_boot_status.return_value = future
            mock_get_repo.return_value = mock_repo

            result = await get_system_status()
            assert result.first_boot_completed is False

    @pytest.mark.asyncio
    async def test_status_returns_true_when_completed(self):
        """Requirement 12.2: returns true when first-boot item exists with completed=true."""
        from apis.app_api.system.routes import get_system_status

        with patch(
            "apis.app_api.system.routes.get_system_settings_repository"
        ) as mock_get_repo:
            mock_repo = MagicMock()
            import asyncio

            future = asyncio.Future()
            future.set_result(
                {"PK": FIRST_BOOT_PK, "SK": FIRST_BOOT_SK, "completed": True}
            )
            mock_repo.get_first_boot_status.return_value = future
            mock_get_repo.return_value = mock_repo

            result = await get_system_status()
            assert result.first_boot_completed is True

    @pytest.mark.asyncio
    async def test_status_returns_false_when_completed_is_false(self):
        """Item exists but completed is False."""
        from apis.app_api.system.routes import get_system_status

        with patch(
            "apis.app_api.system.routes.get_system_settings_repository"
        ) as mock_get_repo:
            mock_repo = MagicMock()
            import asyncio

            future = asyncio.Future()
            future.set_result(
                {"PK": FIRST_BOOT_PK, "SK": FIRST_BOOT_SK, "completed": False}
            )
            mock_repo.get_first_boot_status.return_value = future
            mock_get_repo.return_value = mock_repo

            result = await get_system_status()
            assert result.first_boot_completed is False

    @pytest.mark.asyncio
    async def test_status_returns_false_on_dynamo_failure(self):
        """Requirement 12.5: DynamoDB failure returns safe default false."""
        from apis.app_api.system.routes import get_system_status

        with patch(
            "apis.app_api.system.routes.get_system_settings_repository"
        ) as mock_get_repo:
            mock_repo = MagicMock()
            import asyncio

            future = asyncio.Future()
            future.set_exception(
                ClientError(
                    {"Error": {"Code": "InternalServerError", "Message": "boom"}},
                    "GetItem",
                )
            )
            mock_repo.get_first_boot_status.return_value = future
            mock_get_repo.return_value = mock_repo

            result = await get_system_status()
            assert result.first_boot_completed is False
