"""Tests for FastAPI auth dependencies.

Covers:
- get_current_user: Bearer token validation via CognitoJWTValidator
- get_current_user_trusted: JWT decode without signature verification
- get_current_user_id: convenience wrapper returning user_id string

Requirements: 10.5, 10.6
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import jwt as pyjwt
import pytest
from fastapi import HTTPException

from apis.shared.auth.dependencies import (
    get_current_user,
    get_current_user_id,
    get_current_user_trusted,
)
from apis.shared.auth.models import User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bearer(token: str):
    """Create a mock HTTPAuthorizationCredentials with the given token."""
    creds = MagicMock()
    creds.credentials = token
    return creds


# ---------------------------------------------------------------------------
# get_current_user tests
# ---------------------------------------------------------------------------


class TestGetCurrentUser:
    """Tests for the get_current_user dependency (Cognito-based)."""

    @pytest.mark.asyncio
    async def test_valid_bearer_token(self, make_jwt, make_user):
        """Req 10.5: valid Bearer token validated by CognitoJWTValidator, returns User with raw_token."""
        token = make_jwt()
        expected_user = make_user(raw_token=None)

        mock_validator = MagicMock()
        mock_validator.validate_token = MagicMock(return_value=expected_user)

        with patch(
            "apis.shared.auth.dependencies._get_cognito_validator",
            return_value=mock_validator,
        ), patch(
            "apis.shared.auth.dependencies._get_user_sync_service",
            return_value=None,
        ):
            user = await get_current_user(credentials=_bearer(token))

        assert isinstance(user, User)
        assert user.raw_token == token
        assert user.user_id == expected_user.user_id
        mock_validator.validate_token.assert_called_once_with(token)

    @pytest.mark.asyncio
    async def test_no_credentials_401(self):
        """Req 10.5: None credentials raises 401 with WWW-Authenticate header."""
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=None)

        assert exc_info.value.status_code == 401
        assert "WWW-Authenticate" in (exc_info.value.headers or {})

    @pytest.mark.asyncio
    async def test_failed_validation_401(self, make_jwt):
        """Req 10.5: token that fails Cognito validation raises 401."""
        token = make_jwt()

        mock_validator = MagicMock()
        mock_validator.validate_token = MagicMock(
            side_effect=HTTPException(status_code=401, detail="Invalid token signature.")
        )

        with patch(
            "apis.shared.auth.dependencies._get_cognito_validator",
            return_value=mock_validator,
        ), patch(
            "apis.shared.auth.dependencies._get_user_sync_service",
            return_value=None,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(credentials=_bearer(token))

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_no_validator_500(self, make_jwt):
        """Req 10.6: no Cognito validator available raises 500."""
        token = make_jwt()

        with patch(
            "apis.shared.auth.dependencies._get_cognito_validator",
            return_value=None,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(credentials=_bearer(token))

        assert exc_info.value.status_code == 500
        assert "Authentication service not configured" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_unexpected_exception_401(self, make_jwt):
        """Unexpected exception during validation raises 401."""
        token = make_jwt()

        mock_validator = MagicMock()
        mock_validator.validate_token = MagicMock(
            side_effect=RuntimeError("unexpected")
        )

        with patch(
            "apis.shared.auth.dependencies._get_cognito_validator",
            return_value=mock_validator,
        ), patch(
            "apis.shared.auth.dependencies._get_user_sync_service",
            return_value=None,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(credentials=_bearer(token))

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Authentication failed."


# ---------------------------------------------------------------------------
# get_current_user_trusted tests
# ---------------------------------------------------------------------------


class TestGetCurrentUserTrusted:
    """Tests for the get_current_user_trusted dependency."""

    @pytest.mark.asyncio
    async def test_trusted_decode_success(self, make_jwt):
        """Valid Bearer token decoded without signature verification, returns User."""
        token = make_jwt(
            claims={
                "sub": "trusted-user-001",
                "email": "trusted@example.com",
                "name": "Trusted User",
                "roles": ["Admin"],
            },
        )

        with patch(
            "apis.shared.auth.dependencies._get_user_sync_service",
            return_value=None,
        ):
            user = await get_current_user_trusted(credentials=_bearer(token))

        assert isinstance(user, User)
        assert user.user_id == "trusted-user-001"
        assert user.email == "trusted@example.com"
        assert user.name == "Trusted User"
        assert user.roles == ["Admin"]
        assert user.raw_token == token

    @pytest.mark.asyncio
    async def test_trusted_cognito_groups(self, make_jwt):
        """Trusted path extracts cognito:groups as roles."""
        token = make_jwt(
            claims={
                "sub": "cognito-user-001",
                "email": "cognito@example.com",
                "name": "Cognito User",
                "cognito:groups": ["system_admin", "developer"],
            },
        )

        with patch(
            "apis.shared.auth.dependencies._get_user_sync_service",
            return_value=None,
        ):
            user = await get_current_user_trusted(credentials=_bearer(token))

        assert user.roles == ["system_admin", "developer"]

    @pytest.mark.asyncio
    async def test_trusted_malformed_token(self):
        """Malformed token raises 401 with 'Malformed token.'."""
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user_trusted(credentials=_bearer("not.a.jwt"))

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Malformed token."

    @pytest.mark.asyncio
    async def test_trusted_fallback_claims(self, make_jwt):
        """Standard OIDC claims (sub, email, name, roles) are extracted correctly."""
        token = make_jwt(
            claims={
                "sub": "fallback-user",
                "email": "fallback@example.com",
                "name": "Fallback User",
                "roles": ["Reader"],
            },
        )

        with patch(
            "apis.shared.auth.dependencies._get_user_sync_service",
            return_value=None,
        ):
            user = await get_current_user_trusted(credentials=_bearer(token))

        assert user.user_id == "fallback-user"
        assert user.email == "fallback@example.com"
        assert user.name == "Fallback User"
        assert user.roles == ["Reader"]

    @pytest.mark.asyncio
    async def test_trusted_missing_user_id(self, make_jwt):
        """Missing sub claim raises 401 with 'Invalid user.'."""
        token = make_jwt(
            claims={
                "sub": None,
                "email": "nouser@example.com",
                "name": "No User",
            },
        )

        with patch(
            "apis.shared.auth.dependencies._get_user_sync_service",
            return_value=None,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user_trusted(credentials=_bearer(token))

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Invalid user."

    @pytest.mark.asyncio
    async def test_trusted_no_credentials_401(self):
        """Trusted path also rejects missing credentials with 401."""
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user_trusted(credentials=None)

        assert exc_info.value.status_code == 401
        assert "WWW-Authenticate" in (exc_info.value.headers or {})


# ---------------------------------------------------------------------------
# get_current_user_id tests
# ---------------------------------------------------------------------------


class TestGetCurrentUserId:
    """Tests for the get_current_user_id dependency."""

    @pytest.mark.asyncio
    async def test_returns_string(self, make_jwt, make_user):
        """get_current_user_id returns the user_id string."""
        token = make_jwt()
        expected_user = make_user(user_id="uid-42")

        mock_validator = MagicMock()
        mock_validator.validate_token = MagicMock(return_value=expected_user)

        with patch(
            "apis.shared.auth.dependencies._get_cognito_validator",
            return_value=mock_validator,
        ), patch(
            "apis.shared.auth.dependencies._get_user_sync_service",
            return_value=None,
        ):
            user_id = await get_current_user_id(
                user=await get_current_user(credentials=_bearer(token))
            )

        assert user_id == "uid-42"
        assert isinstance(user_id, str)
