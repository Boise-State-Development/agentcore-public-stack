"""Tests for CognitoJWTValidator.

Covers: valid token decode, issuer verification, client_id/aud verification,
expiration check, claim extraction (sub, email, name, cognito:username fallback,
cognito:groups, custom:roles, picture), invalid signature, and missing sub claim.

Requirements: 10.1, 10.2, 10.3, 10.4
"""

import time
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import jwt as pyjwt
import pytest
from fastapi import HTTPException

from apis.shared.auth.cognito_jwt_validator import CognitoJWTValidator


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_POOL_ID = "us-east-1_TestPool"
APP_CLIENT_ID = "test-app-client-id"
REGION = "us-east-1"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def validator(mock_jwks_client):
    """Create a CognitoJWTValidator with a mocked JWKS client."""
    v = CognitoJWTValidator(
        user_pool_id=USER_POOL_ID,
        app_client_id=APP_CLIENT_ID,
        region=REGION,
    )
    v._jwks_client = mock_jwks_client
    return v


@pytest.fixture
def make_cognito_jwt(rsa_key_pair):
    """Factory that creates signed Cognito-style JWT tokens."""

    def _make(
        claims: Optional[Dict[str, Any]] = None,
        expired: bool = False,
    ) -> str:
        now = int(time.time())
        default_claims: Dict[str, Any] = {
            "sub": "abc-123-def",
            "email": "admin@example.com",
            "name": "Admin User",
            "cognito:username": "adminuser",
            "cognito:groups": ["system_admin"],
            "client_id": APP_CLIENT_ID,
            "iss": ISSUER,
            "iat": now,
            "exp": now - 3600 if expired else now + 3600,
        }
        if claims:
            default_claims.update(claims)

        return pyjwt.encode(
            default_claims,
            rsa_key_pair["private_pem"],
            algorithm="RS256",
            headers={"kid": "test-key-id"},
        )

    return _make


# ---------------------------------------------------------------------------
# Valid token decode
# ---------------------------------------------------------------------------


class TestValidTokenDecode:
    """Validates: Requirements 10.1, 10.2, 10.3, 10.4"""

    def test_valid_access_token_returns_user(self, validator, make_cognito_jwt):
        token = make_cognito_jwt()
        user = validator.validate_token(token)

        assert user.user_id == "abc-123-def"
        assert user.email == "admin@example.com"
        assert user.name == "Admin User"
        assert user.roles == ["system_admin"]

    def test_valid_id_token_with_aud_returns_user(self, validator, make_cognito_jwt):
        """ID tokens use `aud` instead of `client_id`."""
        token = make_cognito_jwt(claims={
            "aud": APP_CLIENT_ID,
            "client_id": None,
        })
        user = validator.validate_token(token)

        assert user.user_id == "abc-123-def"
        assert user.email == "admin@example.com"


# ---------------------------------------------------------------------------
# Issuer verification
# ---------------------------------------------------------------------------


class TestIssuerVerification:
    """Validates: Requirement 10.2"""

    def test_wrong_issuer_raises_401(self, validator, make_cognito_jwt):
        token = make_cognito_jwt(claims={
            "iss": "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_Wrong",
        })
        with pytest.raises(HTTPException) as exc_info:
            validator.validate_token(token)

        assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# Client ID / Audience verification
# ---------------------------------------------------------------------------


class TestClientIdVerification:
    """Validates: Requirement 10.3"""

    def test_wrong_client_id_raises_401(self, validator, make_cognito_jwt):
        token = make_cognito_jwt(claims={"client_id": "wrong-client-id"})
        with pytest.raises(HTTPException) as exc_info:
            validator.validate_token(token)

        assert exc_info.value.status_code == 401
        assert "Invalid token" in exc_info.value.detail

    def test_wrong_aud_raises_401(self, validator, make_cognito_jwt):
        token = make_cognito_jwt(claims={
            "client_id": None,
            "aud": "wrong-audience",
        })
        with pytest.raises(HTTPException) as exc_info:
            validator.validate_token(token)

        assert exc_info.value.status_code == 401

    def test_no_client_id_or_aud_raises_401(self, validator, make_cognito_jwt):
        token = make_cognito_jwt(claims={
            "client_id": None,
            "aud": None,
        })
        with pytest.raises(HTTPException) as exc_info:
            validator.validate_token(token)

        assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# Expiration verification
# ---------------------------------------------------------------------------


class TestExpirationVerification:
    """Validates: Requirement 10.1"""

    def test_expired_token_raises_401(self, validator, make_cognito_jwt):
        token = make_cognito_jwt(expired=True)
        with pytest.raises(HTTPException) as exc_info:
            validator.validate_token(token)

        assert exc_info.value.status_code == 401
        assert "Token expired" in exc_info.value.detail


# ---------------------------------------------------------------------------
# Claim extraction
# ---------------------------------------------------------------------------


class TestClaimExtraction:
    """Validates: Requirement 10.4"""

    def test_name_falls_back_to_cognito_username(self, validator, make_cognito_jwt):
        token = make_cognito_jwt(claims={"name": None})
        user = validator.validate_token(token)

        assert user.name == "adminuser"

    def test_empty_email_defaults_to_empty_string(self, validator, make_cognito_jwt):
        token = make_cognito_jwt(claims={"email": None})
        user = validator.validate_token(token)

        assert user.email == ""

    def test_cognito_groups_extracted_as_roles(self, validator, make_cognito_jwt):
        token = make_cognito_jwt(claims={
            "cognito:groups": ["admin", "editor"],
        })
        user = validator.validate_token(token)

        assert user.roles == ["admin", "editor"]

    def test_custom_roles_extracted_as_comma_separated(self, validator, make_cognito_jwt):
        """Falls back to custom:roles when cognito:groups is absent."""
        token = make_cognito_jwt(claims={
            "cognito:groups": None,
            "custom:roles": "admin,editor",
        })
        user = validator.validate_token(token)

        assert user.roles == ["admin", "editor"]

    def test_no_roles_returns_empty_list(self, validator, make_cognito_jwt):
        token = make_cognito_jwt(claims={
            "cognito:groups": None,
            "custom:roles": None,
        })
        user = validator.validate_token(token)

        assert user.roles == []

    def test_picture_extracted(self, validator, make_cognito_jwt):
        token = make_cognito_jwt(claims={
            "picture": "https://example.com/photo.jpg",
        })
        user = validator.validate_token(token)

        assert user.picture == "https://example.com/photo.jpg"

    def test_picture_none_when_absent(self, validator, make_cognito_jwt):
        token = make_cognito_jwt()
        user = validator.validate_token(token)

        assert user.picture is None


# ---------------------------------------------------------------------------
# Invalid signature
# ---------------------------------------------------------------------------


class TestInvalidSignature:
    """Validates: Requirement 10.1"""

    def test_invalid_signature_raises_401(self, validator, make_cognito_jwt):
        token = make_cognito_jwt()
        bad_client = MagicMock()
        bad_client.get_signing_key_from_jwt = MagicMock(
            side_effect=pyjwt.exceptions.InvalidSignatureError("bad sig")
        )
        validator._jwks_client = bad_client

        with pytest.raises(HTTPException) as exc_info:
            validator.validate_token(token)

        assert exc_info.value.status_code == 401
        assert "Invalid token signature" in exc_info.value.detail


# ---------------------------------------------------------------------------
# Missing sub claim
# ---------------------------------------------------------------------------


class TestMissingSub:
    """Validates: Requirement 10.4"""

    def test_missing_sub_raises_error(self, validator, make_cognito_jwt, rsa_key_pair):
        """Token with 'sub' key removed should raise 401."""
        import time as _time
        import jwt as pyjwt

        now = int(_time.time())
        claims = {
            "email": "test@example.com",
            "name": "Test",
            "client_id": APP_CLIENT_ID,
            "iss": ISSUER,
            "iat": now,
            "exp": now + 3600,
        }
        private_key = rsa_key_pair["private_pem"]
        token = pyjwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key-id"})
        with pytest.raises(HTTPException) as exc_info:
            validator.validate_token(token)
        assert exc_info.value.status_code == 401

    def test_token_without_sub_key_raises_401(self, validator, rsa_key_pair):
        """Token completely missing the 'sub' key."""
        now = int(time.time())
        claims = {
            "email": "test@example.com",
            "name": "Test",
            "client_id": APP_CLIENT_ID,
            "iss": ISSUER,
            "iat": now,
            "exp": now + 3600,
        }
        token = pyjwt.encode(
            claims,
            rsa_key_pair["private_pem"],
            algorithm="RS256",
            headers={"kid": "test-key-id"},
        )

        with pytest.raises(HTTPException) as exc_info:
            validator.validate_token(token)

        assert exc_info.value.status_code == 401
