"""Cognito JWT validator for single-issuer Cognito User Pool authentication."""

import logging
from typing import List

import jwt
from jwt import PyJWKClient
from fastapi import HTTPException, status

from .models import User

logger = logging.getLogger(__name__)


class CognitoJWTValidator:
    """Validates JWT tokens against a single Cognito User Pool.

    Supports both Cognito access tokens (which use `client_id` claim)
    and Cognito ID tokens (which use `aud` claim) for App Client verification.
    """

    def __init__(self, user_pool_id: str, app_client_id: str, region: str):
        self._issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        self._app_client_id = app_client_id
        jwks_url = f"{self._issuer}/.well-known/jwks.json"
        self._jwks_client = PyJWKClient(jwks_url, cache_keys=True)

    def validate_token(self, token: str) -> User:
        """Validate a Cognito JWT token and extract user identity.

        Args:
            token: JWT token string (access or ID token).

        Returns:
            User object with extracted claims.

        Raises:
            HTTPException: If token validation fails.
        """
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            # Cognito access tokens place the App Client ID in `client_id`,
            # not `aud`. We disable PyJWT's built-in aud check and verify manually.
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=self._issuer,
                options={"verify_exp": True, "verify_aud": False},
            )

            # Validate client_id (access tokens) or aud (ID tokens)
            token_client_id = payload.get("client_id") or payload.get("aud")
            if token_client_id != self._app_client_id:
                raise jwt.InvalidTokenError(
                    f"Token client_id/aud '{token_client_id}' does not match "
                    f"expected '{self._app_client_id}'"
                )

            # Extract roles from cognito:groups (list) or custom:roles (comma-separated string)
            roles = self._extract_roles(payload)

            return User(
                user_id=payload["sub"],
                email=payload.get("email") or "",
                name=payload.get("name") or payload.get("cognito:username") or payload.get("username") or "",
                roles=roles,
                picture=payload.get("picture"),
            )

        except jwt.InvalidSignatureError as e:
            logger.error(f"Invalid Cognito token signature: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token signature.",
            )
        except jwt.InvalidIssuerError as e:
            logger.error(f"Invalid Cognito token issuer: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token issuer.",
            )
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token expired. Please refresh your session.",
            )
        except jwt.InvalidTokenError as e:
            logger.error(f"Invalid Cognito token: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token.",
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Cognito token validation failed: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token validation failed.",
            )

    def _extract_roles(self, payload: dict) -> List[str]:
        """Extract roles from Cognito token claims.

        Checks `cognito:groups` (list from Cognito User Pool Groups) first,
        then falls back to `custom:roles` (comma-separated string).
        """
        groups = payload.get("cognito:groups")
        if isinstance(groups, list):
            return groups

        custom_roles = payload.get("custom:roles", "")
        if custom_roles:
            return [r.strip() for r in custom_roles.split(",") if r.strip()]

        return []
