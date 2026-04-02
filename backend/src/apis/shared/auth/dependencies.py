"""FastAPI dependencies for authentication."""

import asyncio
import jwt
import logging
import os
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from .models import User

logger = logging.getLogger(__name__)

# Lazy import to avoid circular dependencies
_user_sync_service = None


def _get_user_sync_service():
    """Get UserSyncService instance, creating it lazily on first use."""
    global _user_sync_service
    if _user_sync_service is None:
        try:
            from apis.shared.users.repository import UserRepository
            from apis.shared.users.sync import UserSyncService
            repository = UserRepository()
            _user_sync_service = UserSyncService(repository=repository)
            if _user_sync_service.enabled:
                logger.info("UserSyncService initialized for JWT sync")
            else:
                logger.debug("UserSyncService disabled - no table configured")
        except Exception as e:
            logger.warning(f"Failed to initialize UserSyncService: {e}")
            _user_sync_service = None
    return _user_sync_service

# HTTP Bearer token security scheme with auto_error=False to handle missing tokens manually
security = HTTPBearer(auto_error=False)


# ─── User Profile Cache ────────────────────────────────────────────────
# Cognito access tokens don't contain identity claims (email, name, picture).
# We cache the user profile from DynamoDB so we only hit the table once per
# user, not on every request.  TTL keeps it fresh if the profile changes.

_user_profile_cache: dict[str, tuple[float, dict]] = {}
_USER_PROFILE_CACHE_TTL = 300  # 5 minutes

_user_repository = None


def _get_user_repository():
    """Get UserRepository instance, creating it lazily on first use."""
    global _user_repository
    if _user_repository is not None:
        return _user_repository
    try:
        from apis.shared.users.repository import UserRepository
        repo = UserRepository()
        if repo.enabled:
            _user_repository = repo
    except Exception as e:
        logger.warning(f"Failed to initialize UserRepository for profile cache: {e}")
    return _user_repository


async def _enrich_user_from_store(user: User) -> None:
    """Fill in missing identity claims (email, name) from the Users DynamoDB table.

    Cognito access tokens only carry sub, cognito:groups, and username.
    The Users table (populated during first-boot / user sync) stores the
    full profile.  Results are cached in-memory to avoid per-request lookups.
    """
    import time

    # Nothing to do if the token already has email
    if user.email:
        return

    # Check cache
    now = time.monotonic()
    cached = _user_profile_cache.get(user.user_id)
    if cached:
        ts, profile = cached
        if now - ts < _USER_PROFILE_CACHE_TTL:
            user.email = profile.get("email", user.email) or user.email
            user.name = profile.get("name", user.name) or user.name
            return

    # Cache miss — query DynamoDB
    repo = _get_user_repository()
    if not repo:
        return

    try:
        stored = await repo.get_user_by_user_id(user.user_id)
        if stored:
            profile = {"email": stored.email, "name": stored.name}
            _user_profile_cache[user.user_id] = (now, profile)
            user.email = stored.email or user.email
            user.name = stored.name or user.name
    except Exception as e:
        logger.debug(f"Profile enrichment failed for {user.user_id}: {e}")


async def _sync_user_background(sync_service, user: User) -> None:
    """Sync user to DynamoDB in the background (fire-and-forget)."""
    try:
        await sync_service.sync_user_from_jwt(user)
        logger.debug(f"Synced user {user.user_id} to Users table")
    except Exception as e:
        # Log but don't fail - sync should never break authentication
        logger.warning(f"Failed to sync user {user.user_id}: {e}")

# Lazy-initialized Cognito validator singleton
_cognito_validator = None


def _get_cognito_validator():
    """
    Get the CognitoJWTValidator singleton instance.

    Reads Cognito configuration from environment variables:
    - COGNITO_USER_POOL_ID: The Cognito User Pool ID
    - COGNITO_APP_CLIENT_ID: The Cognito App Client ID
    - COGNITO_REGION or AWS_REGION: The AWS region

    Returns None if required environment variables are not set.
    """
    global _cognito_validator
    if _cognito_validator is not None:
        return _cognito_validator

    try:
        from .cognito_jwt_validator import CognitoJWTValidator

        user_pool_id = os.environ.get("COGNITO_USER_POOL_ID")
        app_client_id = os.environ.get("COGNITO_APP_CLIENT_ID")
        region = os.environ.get("COGNITO_REGION") or os.environ.get("AWS_REGION")

        if not user_pool_id or not app_client_id or not region:
            logger.warning(
                "Cognito environment variables not fully configured. "
                "Required: COGNITO_USER_POOL_ID, COGNITO_APP_CLIENT_ID, "
                "COGNITO_REGION (or AWS_REGION)"
            )
            return None

        _cognito_validator = CognitoJWTValidator(
            user_pool_id=user_pool_id,
            app_client_id=app_client_id,
            region=region,
        )
        logger.info("CognitoJWTValidator initialized for Cognito auth")
    except Exception as e:
        logger.error(f"Failed to initialize CognitoJWTValidator: {e}", exc_info=True)

    return _cognito_validator


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> User:
    """
    FastAPI dependency to get the current authenticated user.

    Validates the JWT token using the CognitoJWTValidator against
    the configured Cognito User Pool.

    Args:
        credentials: HTTP Bearer token credentials (None if missing)

    Returns:
        User object with authenticated user information

    Raises:
        HTTPException:
            - 401 if token is missing or invalid
            - 500 if no JWT validator is available
    """
    # Check if credentials are missing
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please provide a valid Bearer token in the Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    validator = _get_cognito_validator()
    if validator:
        try:
            user = validator.validate_token(token)
            user.raw_token = token

            # Enrich with stored profile (email, name) when using access tokens
            await _enrich_user_from_store(user)

            # Fire-and-forget sync to Users table
            sync_service = _get_user_sync_service()
            if sync_service and sync_service.enabled:
                asyncio.create_task(_sync_user_background(sync_service, user))

            return user
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Token validation failed: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication failed."
            )

    # No validator available - Cognito not configured
    logger.error("No JWT validator available. Ensure Cognito environment variables are configured.")
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Authentication service not configured. Cognito environment variables are missing."
    )


async def get_current_user_id(
    user: User = Depends(get_current_user)
) -> str:
    """
    FastAPI dependency to get the current user's ID as a string.

    This is a convenience wrapper around get_current_user that extracts
    just the user_id field. Useful when you only need the user ID and not
    the full User object.

    Args:
        user: User object from get_current_user dependency

    Returns:
        User ID string
    """
    return user.user_id


async def get_current_user_trusted(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> User:
    """
    FastAPI dependency to get current user from pre-validated JWT.

    Use this when JWT validation is already performed at the network level
    (e.g., by AWS Bedrock AgentCore Runtime's JWT authorizer). This method
    skips expensive signature verification and simply extracts standard
    Cognito/OIDC claims from the token.

    Security: Only use this in services where the JWT validation
    is guaranteed. IE AgentCore Runtime with Inbound Auth. For services without pre-validation, use
    get_current_user() instead.

    Args:
        credentials: HTTP Bearer token credentials (None if missing)

    Returns:
        User object with authenticated user information

    Raises:
        HTTPException:
            - 401 if token is missing or malformed
    """
    logger.debug("[get_current_user_trusted] Trusted auth extraction started")

    # Check if credentials are missing
    if credentials is None:
        logger.debug("[get_current_user_trusted] No credentials provided - returning 401")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please provide a valid Bearer token in the Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    try:
        # Decode JWT without verification (network layer already validated it)
        payload = jwt.decode(token, options={"verify_signature": False})
        logger.debug("[get_current_user_trusted] JWT decoded successfully")

        # Extract standard Cognito/OIDC claims
        email = payload.get('email') or payload.get('preferred_username')
        name = payload.get('name') or (
            f"{payload.get('given_name', '')} {payload.get('family_name', '')}"
        ).strip() or payload.get('cognito:username') or payload.get('username') or ""
        user_id = payload.get('sub')
        # Support cognito:groups (list) or roles claim
        roles = payload.get('cognito:groups') or payload.get('roles', [])
        picture = payload.get('picture')

        logger.debug("[get_current_user_trusted] Claims extracted from token")

        if not user_id:
            logger.error("[get_current_user_trusted] Missing 'sub' claim in token - returning 401")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid user."
            )

        if isinstance(roles, str):
            roles = [roles]

        user = User(
            email=email.lower() if email else "",
            name=name,
            user_id=str(user_id),
            roles=roles,
            picture=picture,
            raw_token=token,
        )

        logger.debug("[get_current_user_trusted] User authenticated successfully")

        # Enrich with stored profile (email, name) when using access tokens
        await _enrich_user_from_store(user)

        # Fire-and-forget sync to Users table
        sync_service = _get_user_sync_service()
        if sync_service and sync_service.enabled:
            asyncio.create_task(_sync_user_background(sync_service, user))

        return user

    except jwt.DecodeError as e:
        logger.error(f"[get_current_user_trusted] Failed to decode JWT token: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed token."
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[get_current_user_trusted] Unexpected error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed."
        )
