"""Tests for the login-page settings: storage, resolution, and fail-open reads."""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from apis.app_api.system.login_page import (
    cognito_login_visible,
    read_hide_cognito_login,
)
from apis.app_api.system.repository import (
    LOGIN_PAGE_PK,
    LOGIN_PAGE_SK,
    SystemSettingsRepository,
)


def _make_repo() -> SystemSettingsRepository:
    with patch("apis.app_api.system.repository.boto3") as mock_boto:
        mock_table = MagicMock()
        mock_boto.resource.return_value.Table.return_value = mock_table
        mock_boto.Session.return_value.resource.return_value.Table.return_value = mock_table
        repo = SystemSettingsRepository(table_name="test-table")
    repo._table = mock_table
    return repo


class TestCognitoLoginVisible:
    @pytest.mark.parametrize(
        ("hide_requested", "enabled_provider_count", "expected"),
        [
            (False, 0, True),
            (False, 2, True),
            (True, 1, False),
            # Never hide the only way in.
            (True, 0, True),
        ],
    )
    def test_resolution(self, hide_requested, enabled_provider_count, expected):
        assert (
            cognito_login_visible(
                hide_requested=hide_requested,
                enabled_provider_count=enabled_provider_count,
            )
            is expected
        )


class TestLoginPageSettingsRepository:
    @pytest.mark.asyncio
    async def test_get_returns_none_when_never_saved(self):
        repo = _make_repo()
        repo._table.get_item.return_value = {}

        assert await repo.get_login_page_settings() is None
        repo._table.get_item.assert_called_once_with(
            Key={"PK": LOGIN_PAGE_PK, "SK": LOGIN_PAGE_SK}
        )

    @pytest.mark.asyncio
    async def test_put_writes_the_single_item(self):
        repo = _make_repo()

        item = await repo.put_login_page_settings(
            hide_cognito_login=True, updated_by="user-001"
        )

        written = repo._table.put_item.call_args.kwargs["Item"]
        assert written == item
        assert written["PK"] == LOGIN_PAGE_PK
        assert written["SK"] == LOGIN_PAGE_SK
        assert written["hideCognitoLogin"] is True
        assert written["updatedBy"] == "user-001"
        assert written["updatedAt"]

    @pytest.mark.asyncio
    async def test_item_is_not_an_auth_provider_row(self):
        """Provider listings scan for SK prefix AUTH_PROVIDER#; this row must not match."""
        assert not LOGIN_PAGE_SK.startswith("AUTH_PROVIDER#")

    @pytest.mark.asyncio
    async def test_put_refuses_when_disabled(self):
        with patch.dict("os.environ", {"DYNAMODB_AUTH_PROVIDERS_TABLE_NAME": ""}):
            repo = SystemSettingsRepository(table_name="")
        with pytest.raises(RuntimeError):
            await repo.put_login_page_settings(
                hide_cognito_login=True, updated_by="user-001"
            )


class TestReadHideCognitoLogin:
    @pytest.mark.asyncio
    async def test_false_when_never_saved(self):
        repo = _make_repo()
        repo._table.get_item.return_value = {}
        with patch(
            "apis.app_api.system.login_page.get_system_settings_repository",
            return_value=repo,
        ):
            assert await read_hide_cognito_login() is False

    @pytest.mark.asyncio
    async def test_true_when_saved(self):
        repo = _make_repo()
        repo._table.get_item.return_value = {"Item": {"hideCognitoLogin": True}}
        with patch(
            "apis.app_api.system.login_page.get_system_settings_repository",
            return_value=repo,
        ):
            assert await read_hide_cognito_login() is True

    @pytest.mark.asyncio
    async def test_read_failure_shows_cognito(self):
        repo = _make_repo()
        repo._table.get_item.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
            "GetItem",
        )
        with patch(
            "apis.app_api.system.login_page.get_system_settings_repository",
            return_value=repo,
        ):
            assert await read_hide_cognito_login() is False

    @pytest.mark.asyncio
    async def test_false_when_storage_not_configured(self):
        repo = MagicMock()
        repo.enabled = False
        with patch(
            "apis.app_api.system.login_page.get_system_settings_repository",
            return_value=repo,
        ):
            assert await read_hide_cognito_login() is False
