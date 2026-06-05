"""FastAPI dependencies for authentication."""

import asyncio
import jwt
import logging
import os
from typing import Optional
from fastapi import Depends, HTTPException, Request, status
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
#
# Each entry is also tagged with the roles-version watermark current at the
# time it was stored. When an admin mutates a role definition (or invokes
# the global cache-invalidate route), the watermark advances and every
# subsequent profile read re-queries DynamoDB until a fresh entry is
# stored — eliminating the "old roles persist for ~5 minutes" window that
# the TTL alone would otherwise create.

_user_profile_cache: dict[str, tuple[float, int, dict]] = {}
_USER_PROFILE_CACHE_TTL = 300  # 5 minutes


def invalidate_user_profile_cache(user_id: str) -> None:
    """Remove a user's cached profile so the next request re-reads from DynamoDB.

    Call this after updating the Users table (e.g. from /users/me/sync) so
    that subsequent requests pick up the fresh roles immediately.
    """
    _user_profile_cache.pop(user_id, None)

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
    """Fill in missing identity claims from the Users DynamoDB table.

    Cognito access tokens only carry sub, cognito:groups, and username.
    The Users table (populated by the frontend's /users/me/sync call
    which decodes the ID token) stores the full profile including the
    IdP roles mapped via custom:roles.

    This enrichment is critical for RBAC: the access token's
    cognito:groups contains the Cognito provider group name (e.g.
    ``us-west-2_Pool_provider-name``), not the actual IdP roles.
    The stored profile has the real roles parsed from the ID token.

    Results are cached in-memory to avoid per-request DynamoDB lookups.
    """
    import time

    from apis.shared.rbac.version import get_roles_version

    current_version = get_roles_version()

    # Check cache first
    now = time.monotonic()
    cached = _user_profile_cache.get(user.user_id)
    if cached:
        ts, cached_version, profile = cached
        if now - ts < _USER_PROFILE_CACHE_TTL and cached_version == current_version:
            user.email = profile.get("email") or user.email
            user.name = profile.get("name") or user.name
            stored_roles = profile.get("roles")
            if stored_roles:
                user.roles = stored_roles
            return

    # Cache miss — query DynamoDB
    repo = _get_user_repository()
    if not repo:
        return

    try:
        stored = await repo.get_user_by_user_id(user.user_id)
        if stored:
            profile = {
                "email": stored.email,
                "name": stored.name,
                "roles": stored.roles,
            }
            _user_profile_cache[user.user_id] = (now, current_version, profile)
            user.email = stored.email or user.email
            user.name = stored.name or user.name
            if stored.roles:
                user.roles = stored.roles
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

# Cognito JWT validator for tokens minted by the BFF confidential client.
# The SPA-public PKCE client was decommissioned in Phase 7; all browser-facing
# auth now flows through the BFF, which issues tokens carrying
# `client_id = COGNITO_BFF_APP_CLIENT_ID`.
_bff_cognito_validator = None


def _get_bff_cognito_validator():
    """Return the validator instance for tokens minted by the BFF flow.

    Reads `COGNITO_USER_POOL_ID`, `COGNITO_BFF_APP_CLIENT_ID`, and
    `COGNITO_REGION`/`AWS_REGION`. Returns None if any are unset, which
    matches the dormant-by-default contract: until Phase 1 env vars are
    deployed there's nothing for this validator to do.
    """
    global _bff_cognito_validator
    if _bff_cognito_validator is not None:
        return _bff_cognito_validator

    try:
        from .cognito_jwt_validator import CognitoJWTValidator

        user_pool_id = os.environ.get("COGNITO_USER_POOL_ID")
        bff_app_client_id = os.environ.get("COGNITO_BFF_APP_CLIENT_ID")
        region = os.environ.get("COGNITO_REGION") or os.environ.get("AWS_REGION")

        if not user_pool_id or not bff_app_client_id or not region:
            logger.warning(
                "BFF Cognito validator not configured. "
                "Required: COGNITO_USER_POOL_ID, COGNITO_BFF_APP_CLIENT_ID, "
                "COGNITO_REGION (or AWS_REGION)"
            )
            return None

        _bff_cognito_validator = CognitoJWTValidator(
            user_pool_id=user_pool_id,
            app_client_id=bff_app_client_id,
            region=region,
        )
        logger.info("BFF CognitoJWTValidator initialized")
    except Exception as e:
        logger.error(f"Failed to initialize BFF CognitoJWTValidator: {e}", exc_info=True)

    return _bff_cognito_validator


def _skip_auth_user() -> Optional[User]:
    """Return a fake admin user when SKIP_AUTH=true, else None.

    Local-dev-only bypass so an unattended agent (or a dev with no IdP
    access) can hit protected routes without the OAuth round-trip. The
    startup check in `app_api/main.py` refuses to boot when this is
    combined with deployed-environment indicators.
    """
    if os.environ.get("SKIP_AUTH", "").lower() != "true":
        return None
    roles = [
        r.strip()
        for r in os.environ.get("SKIP_AUTH_ROLES", "admin").split(",")
        if r.strip()
    ]
    return User(
        user_id=os.environ.get("SKIP_AUTH_USER_ID", "local-dev"),
        email=os.environ.get("SKIP_AUTH_EMAIL", "dev@local"),
        name="Local Dev",
        roles=roles,
    )


async def get_current_user_from_session(request: Request) -> User:
    """FastAPI dependency to authenticate via the BFF session cookie.

    `SessionRefreshMiddleware` is responsible for unsealing the cookie,
    looking up the session row, refreshing the access token if needed, and
    attaching the resulting `SessionRecord` to `request.state.bff_session`.
    This dependency consumes that and runs the access token through the BFF
    Cognito validator + profile-enrichment pipeline so RBAC, the user-profile
    cache, and the fire-and-forget user-sync background task all behave
    consistently with the rest of the auth surface.

    This is the only user-facing auth dependency in `app_api/`. External
    Bearer callers were retired in the BFF migration (API keys and voice
    handle their own auth).

    Raises:
        HTTPException 401 if no session was resolved by the upstream
        middleware (cookie missing, malformed, or session record gone).
    """
    if (fake := _skip_auth_user()) is not None:
        return fake

    record = getattr(request.state, "bff_session", None)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No active session.",
        )

    # BFF tokens are issued by the confidential BFF app client, not the SPA's
    # public client — they carry `client_id = COGNITO_BFF_APP_CLIENT_ID` and
    # would fail the SPA validator's client_id check.
    validator = _get_bff_cognito_validator()
    if validator is None:
        # Same failure mode as the Bearer path: a misconfigured environment
        # is a 500, not a 401, so it's noisy enough to surface.
        logger.error("No JWT validator available for session-cookie auth.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication service not configured.",
        )

    try:
        # Re-validate signature on every request rather than trusting the
        # cached row — defense in depth against a compromised DDB row.
        user = validator.validate_token(record.cognito_access_token)
    except HTTPException:
        # An expired/invalid token at this point means the refresh middleware
        # could not heal the session — treat as no active session.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session token rejected.",
        )
    except Exception as exc:
        logger.error("Session-cookie token validation failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed.",
        )

    user.raw_token = record.cognito_access_token
    await _enrich_user_from_store(user)

    sync_service = _get_user_sync_service()
    if sync_service and sync_service.enabled:
        asyncio.create_task(_sync_user_background(sync_service, user))

    return user


async def get_current_user_id(
    user: User = Depends(get_current_user_from_session)
) -> str:
    """FastAPI dependency to get the current user's ID as a string.

    Convenience wrapper around `get_current_user_from_session` that returns
    just the user_id field. Cookie-only since Phase 7 removed the Bearer
    fallback for SPA-facing routes.
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

    Security: Only use this in services where JWT validation is
    guaranteed at the network layer (e.g. AgentCore Runtime with Inbound
    Auth). For cookie-authenticated user-facing routes use
    `get_current_user_from_session` instead.

    Args:
        credentials: HTTP Bearer token credentials (None if missing)

    Returns:
        User object with authenticated user information

    Raises:
        HTTPException:
            - 401 if token is missing or malformed
    """
    logger.debug("[get_current_user_trusted] Trusted auth extraction started")

    if (fake := _skip_auth_user()) is not None:
        return fake

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
