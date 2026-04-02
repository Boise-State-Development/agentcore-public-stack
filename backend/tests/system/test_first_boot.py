"""Unit tests for the POST /system/first-boot endpoint and CognitoService."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from apis.app_api.system.cognito_service import CognitoService

pytestmark = pytest.mark.asyncio


# =========================================================================
# CognitoService tests
# =========================================================================


class TestCognitoService:
    """Unit tests for CognitoService."""

    def _make_service(self) -> CognitoService:
        with patch("apis.app_api.system.cognito_service.boto3") as mock_boto:
            mock_client = MagicMock()
            mock_boto.client.return_value = mock_client
            svc = CognitoService(user_pool_id="us-east-1_TestPool")
            svc._client = mock_client
            return svc

    def test_disabled_when_no_pool_id(self):
        with patch.dict("os.environ", {}, clear=True):
            svc = CognitoService(user_pool_id=None)
        assert svc.enabled is False

    def test_enabled_when_pool_id_set(self):
        svc = self._make_service()
        assert svc.enabled is True
        assert svc.user_pool_id == "us-east-1_TestPool"

    def test_create_admin_user_success(self):
        svc = self._make_service()
        svc._client.admin_create_user.return_value = {
            "User": {
                "Attributes": [
                    {"Name": "sub", "Value": "abc-123-def"},
                    {"Name": "email", "Value": "admin@example.com"},
                ]
            }
        }

        user_sub = svc.create_admin_user("admin", "admin@example.com", "P@ssw0rd!")
        assert user_sub == "abc-123-def"

        svc._client.admin_create_user.assert_called_once_with(
            UserPoolId="us-east-1_TestPool",
            Username="admin",
            UserAttributes=[
                {"Name": "email", "Value": "admin@example.com"},
                {"Name": "email_verified", "Value": "true"},
            ],
            MessageAction="SUPPRESS",
        )
        svc._client.admin_set_user_password.assert_called_once_with(
            UserPoolId="us-east-1_TestPool",
            Username="admin",
            Password="P@ssw0rd!",
            Permanent=True,
        )

    def test_create_admin_user_raises_on_disabled(self):
        with patch.dict("os.environ", {}, clear=True):
            svc = CognitoService(user_pool_id=None)
        with pytest.raises(RuntimeError, match="not enabled"):
            svc.create_admin_user("admin", "a@b.com", "pass")

    def test_delete_user_calls_admin_delete(self):
        svc = self._make_service()
        svc.delete_user("admin")
        svc._client.admin_delete_user.assert_called_once_with(
            UserPoolId="us-east-1_TestPool",
            Username="admin",
        )

    def test_delete_user_swallows_errors(self):
        svc = self._make_service()
        svc._client.admin_delete_user.side_effect = ClientError(
            {"Error": {"Code": "UserNotFoundException", "Message": ""}},
            "AdminDeleteUser",
        )
        # Should not raise
        svc.delete_user("admin")

    def test_disable_self_signup(self):
        svc = self._make_service()
        svc.disable_self_signup()
        svc._client.update_user_pool.assert_called_once()
        call_kwargs = svc._client.update_user_pool.call_args[1]
        assert call_kwargs["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True

    def test_disable_self_signup_raises_on_disabled(self):
        with patch.dict("os.environ", {}, clear=True):
            svc = CognitoService(user_pool_id=None)
        with pytest.raises(RuntimeError, match="not enabled"):
            svc.disable_self_signup()


# =========================================================================
# First-boot endpoint tests
# =========================================================================


class TestFirstBootEndpoint:
    """Validates: Requirements 2.3, 2.4, 2.5, 2.6, 2.7, 2.8."""

    def _make_mocks(self):
        """Create mocked dependencies for the first-boot endpoint."""
        mock_settings_repo = MagicMock()
        mock_cognito = MagicMock()
        mock_user_repo = MagicMock()
        mock_user_repo.enabled = True

        # Default: first-boot not yet completed
        future_none = asyncio.Future()
        future_none.set_result(None)
        mock_settings_repo.get_first_boot_status.return_value = future_none

        # Default: mark completed succeeds
        future_mark = asyncio.Future()
        future_mark.set_result(None)
        mock_settings_repo.mark_first_boot_completed.return_value = future_mark

        # Default: Cognito create succeeds
        mock_cognito.create_admin_user.return_value = "sub-uuid-123"
        mock_cognito.enabled = True

        # Default: user repo create succeeds
        future_create = asyncio.Future()
        future_create.set_result(None)
        mock_user_repo.create_user.return_value = future_create

        return mock_settings_repo, mock_cognito, mock_user_repo

    async def _call_first_boot(
        self, mock_settings_repo, mock_cognito, mock_user_repo,
        username="admin", email="admin@example.com", password="Str0ng!Pass1",
    ):
        """Call the first_boot endpoint with mocked dependencies."""
        from apis.app_api.system.models import FirstBootRequest
        from apis.app_api.system.routes import first_boot

        request = FirstBootRequest(
            username=username, email=email, password=password
        )

        with patch(
            "apis.app_api.system.routes.get_system_settings_repository",
            return_value=mock_settings_repo,
        ), patch(
            "apis.app_api.system.routes.get_cognito_service",
            return_value=mock_cognito,
        ), patch(
            "apis.app_api.system.routes.UserRepository",
            return_value=mock_user_repo,
        ):
            return await first_boot(request)

    async def test_successful_first_boot(self):
        """Req 2.3, 2.4, 2.5: creates Cognito user, DynamoDB record, marks completed."""
        mock_settings, mock_cognito, mock_user_repo = self._make_mocks()

        result = await self._call_first_boot(mock_settings, mock_cognito, mock_user_repo)

        assert result.success is True
        assert result.user_id == "sub-uuid-123"
        mock_cognito.create_admin_user.assert_called_once_with(
            username="admin", email="admin@example.com", password="Str0ng!Pass1"
        )
        mock_user_repo.create_user.assert_called_once()
        # Verify the user profile has system_admin role
        created_profile = mock_user_repo.create_user.call_args[0][0]
        assert "system_admin" in created_profile.roles
        mock_settings.mark_first_boot_completed.assert_called_once()
        mock_cognito.disable_self_signup.assert_called_once()

    async def test_rejects_when_already_completed(self):
        """Req 2.7: returns 409 if first-boot already done."""
        mock_settings, mock_cognito, mock_user_repo = self._make_mocks()

        future_completed = asyncio.Future()
        future_completed.set_result({"completed": True})
        mock_settings.get_first_boot_status.return_value = future_completed

        with pytest.raises(HTTPException) as exc_info:
            await self._call_first_boot(mock_settings, mock_cognito, mock_user_repo)
        assert exc_info.value.status_code == 409

        # Cognito should never be called
        mock_cognito.create_admin_user.assert_not_called()

    async def test_returns_400_on_invalid_password(self):
        """Req 2.8: returns 400 when Cognito rejects the password."""
        mock_settings, mock_cognito, mock_user_repo = self._make_mocks()
        mock_cognito.create_admin_user.side_effect = ClientError(
            {"Error": {"Code": "InvalidPasswordException", "Message": "Too weak"}},
            "AdminCreateUser",
        )

        with pytest.raises(HTTPException) as exc_info:
            await self._call_first_boot(mock_settings, mock_cognito, mock_user_repo)
        assert exc_info.value.status_code == 400

    async def test_returns_409_on_username_exists(self):
        """Returns 409 when Cognito username already exists."""
        mock_settings, mock_cognito, mock_user_repo = self._make_mocks()
        mock_cognito.create_admin_user.side_effect = ClientError(
            {"Error": {"Code": "UsernameExistsException", "Message": "exists"}},
            "AdminCreateUser",
        )

        with pytest.raises(HTTPException) as exc_info:
            await self._call_first_boot(mock_settings, mock_cognito, mock_user_repo)
        assert exc_info.value.status_code == 409

    async def test_rolls_back_cognito_on_user_repo_failure(self):
        """Rollback: deletes Cognito user if DynamoDB user creation fails."""
        mock_settings, mock_cognito, mock_user_repo = self._make_mocks()

        future_fail = asyncio.Future()
        future_fail.set_exception(RuntimeError("DynamoDB write failed"))
        mock_user_repo.create_user.return_value = future_fail

        with pytest.raises(HTTPException) as exc_info:
            await self._call_first_boot(mock_settings, mock_cognito, mock_user_repo)
        assert exc_info.value.status_code == 500

        mock_cognito.delete_user.assert_called_once_with("admin")

    async def test_rolls_back_cognito_on_conditional_check_failure(self):
        """Race condition: conditional write fails → 409 + rollback."""
        mock_settings, mock_cognito, mock_user_repo = self._make_mocks()

        future_race = asyncio.Future()
        future_race.set_exception(
            ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}},
                "PutItem",
            )
        )
        mock_settings.mark_first_boot_completed.return_value = future_race

        with pytest.raises(HTTPException) as exc_info:
            await self._call_first_boot(mock_settings, mock_cognito, mock_user_repo)
        assert exc_info.value.status_code == 409

        mock_cognito.delete_user.assert_called_once_with("admin")

    async def test_disable_self_signup_failure_is_non_fatal(self):
        """Req 2.6: disable_self_signup failure doesn't fail the endpoint."""
        mock_settings, mock_cognito, mock_user_repo = self._make_mocks()
        mock_cognito.disable_self_signup.side_effect = ClientError(
            {"Error": {"Code": "InternalErrorException", "Message": "oops"}},
            "UpdateUserPool",
        )

        result = await self._call_first_boot(mock_settings, mock_cognito, mock_user_repo)
        # Should still succeed
        assert result.success is True
        assert result.user_id == "sub-uuid-123"

    async def test_user_profile_has_correct_fields(self):
        """Req 2.4: user record has system_admin role and correct fields."""
        mock_settings, mock_cognito, mock_user_repo = self._make_mocks()

        await self._call_first_boot(
            mock_settings, mock_cognito, mock_user_repo,
            username="myadmin", email="myadmin@corp.io", password="Str0ng!Pass1",
        )

        created_profile = mock_user_repo.create_user.call_args[0][0]
        assert created_profile.user_id == "sub-uuid-123"
        assert created_profile.email == "myadmin@corp.io"
        assert created_profile.name == "myadmin"
        assert created_profile.roles == ["system_admin"]
        assert created_profile.email_domain == "corp.io"
        assert created_profile.status == "active"
