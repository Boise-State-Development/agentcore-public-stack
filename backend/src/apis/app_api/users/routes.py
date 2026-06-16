"""Users API routes for non-admin user operations."""

from fastapi import APIRouter, HTTPException, Depends, Query
import logging

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.rbac.service import get_app_role_service
from apis.shared.users.repository import UserRepository
from apis.shared.users.models import UserProfile, UserStatus
from .models import UserSearchResult, UserSearchResponse, UserPermissionsResponse, UserProfileSyncRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


def get_user_repository() -> UserRepository:
    """Get user repository instance."""
    return UserRepository()


@router.get("/me/permissions", response_model=UserPermissionsResponse)
async def get_my_permissions(
    current_user: User = Depends(get_current_user_from_session),
):
    """
    Get the current user's effective permissions resolved from AppRoles.

    Any authenticated user can query their own permissions. Resolves JWT roles
    to AppRoles via the RBAC system and returns merged effective permissions.
    """
    logger.info(f"GET /users/me/permissions - User: {current_user.user_id}")
    try:
        service = get_app_role_service()
        permissions = await service.resolve_user_permissions(current_user)
        return UserPermissionsResponse(
            app_roles=permissions.app_roles,
            tools=permissions.tools,
            models=permissions.models,
            quota_tier=permissions.quota_tier,
            resolved_at=permissions.resolved_at,
        )
    except Exception as e:
        logger.error(f"Failed to resolve permissions for {current_user.user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to resolve user permissions")


@router.post("/me/sync", status_code=204)
async def sync_my_profile(
    body: UserProfileSyncRequest,
    current_user: User = Depends(get_current_user_from_session),
    user_repo: UserRepository = Depends(get_user_repository),
):
    """
    Sync user profile from the frontend ID token to the Users table.

    Called by the frontend after each login or token refresh. The ID token
    contains identity claims (email, name, picture) that the access token
    lacks. This keeps the Users table current so the backend can resolve
    email for features like assistant sharing and fine-tuning access.

    Identity-display fields only. ``roles`` and ``email`` are sourced
    exclusively from the validated session (i.e. the JWT-derived
    ``current_user.roles`` / ``current_user.email``) and are never read
    from the request body. Both are populated server-side by the BFF
    callback's ``_sync_user_from_id_token`` off the ID token; this
    endpoint refreshes the remaining display fields (name, picture)
    without granting clients any influence over authorization.
    """
    if not user_repo.enabled:
        return

    email = (current_user.email or "").strip().lower()
    if not email:
        # Indicates the BFF callback never seeded the Users row for this
        # session. The user has a valid token but no IdP-issued email
        # claim is reachable — refuse rather than persist an empty row.
        raise HTTPException(status_code=422, detail="Authenticated session is missing an email claim")

    # Surface clients still sending dropped fields so we can chase down
    # stale code.  ``extra="allow"`` exposes them via ``model_extra``;
    # the values are not acted on either way.
    extras = getattr(body, "model_extra", None) or {}
    legacy_fields = [k for k in ("roles", "email") if k in extras]
    if legacy_fields:
        logger.warning(
            "Profile sync request from %s included legacy fields %s; ignoring (sourced from JWT only)",
            current_user.user_id,
            legacy_fields,
        )

    email_domain = email.split("@")[1] if "@" in email else ""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat() + "Z"

    profile = UserProfile(
        user_id=current_user.user_id,
        email=email,
        name=body.name or current_user.name,
        # Always use JWT-derived roles, never the request body.
        roles=current_user.roles or [],
        picture=body.picture,
        email_domain=email_domain,
        created_at=now,
        last_login_at=now,
        status=UserStatus.ACTIVE,
    )

    try:
        await user_repo.upsert_user(profile)
        # Invalidate the in-memory profile cache so the enrichment function
        # picks up the fresh roles on the very next request.
        from apis.shared.auth.dependencies import invalidate_user_profile_cache

        invalidate_user_profile_cache(current_user.user_id)
        logger.info("Synced profile for user %s", current_user.user_id)
    except Exception as e:
        logger.error(f"Failed to sync profile for {current_user.user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to sync profile")


@router.get("/search", response_model=UserSearchResponse)
async def search_users(
    q: str = Query(..., description="Search query (email or name, partial match)"),
    limit: int = Query(20, ge=1, le=50, description="Maximum number of results"),
    current_user: User = Depends(get_current_user_from_session),
    user_repo: UserRepository = Depends(get_user_repository),
):
    """
    Search for users by email or name (partial match).

    This endpoint is used by the sharing modal to find existing users in the system.
    Only returns active users. Search matches against:
    - Email prefix (case-insensitive)
    - Name contains (case-insensitive)

    Requires JWT authentication. Available to all authenticated users (not admin-only).

    Args:
        q: Search query string
        limit: Maximum number of results (1-50, default 20)
        current_user: Authenticated user from JWT token (injected by dependency)
        user_repo: User repository instance (injected by dependency)

    Returns:
        UserSearchResponse with list of matching users

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 500 if server error
    """
    logger.info("GET /users/search")

    if not user_repo.enabled:
        logger.debug("User repository not enabled - returning empty results")
        return UserSearchResponse(users=[])

    try:
        # Normalize search query
        query_lower = q.lower().strip()

        if not query_lower:
            return UserSearchResponse(users=[])

        # Search by email prefix using EmailIndex
        # Note: DynamoDB doesn't support contains queries efficiently, so we'll use prefix matching
        # For name matching, we'll need to scan/query and filter (less efficient but acceptable for small orgs)

        results = []
        seen_user_ids = set()

        # Try exact email match first (most common case)
        user_by_email = await user_repo.get_user_by_email(query_lower)
        if user_by_email and user_by_email.status == UserStatus.ACTIVE:
            if user_by_email.user_id not in seen_user_ids:
                results.append(UserSearchResult(user_id=user_by_email.user_id, email=user_by_email.email, name=user_by_email.name))
                seen_user_ids.add(user_by_email.user_id)

        # If we already have enough results from exact match, return early
        if len(results) >= limit:
            return UserSearchResponse(users=results[:limit])

        # Search by email prefix (if query looks like email start)
        # Note: DynamoDB EmailIndex doesn't support begins_with efficiently without a scan
        # For now, we'll use a simplified approach: if query contains @, try exact match
        # For broader search, we'd need to implement a scan with filter (acceptable for small user bases)

        # Search by name contains (scan active users)
        # This is less efficient but necessary for name-based search
        # We'll limit to first 100 active users and filter by name
        active_users, _ = await user_repo.list_users_by_status(
            status=UserStatus.ACTIVE.value, limit=100, last_evaluated_key=None  # Scan up to 100 active users
        )

        # Filter by name contains (case-insensitive)
        for user in active_users:
            if user.user_id in seen_user_ids:
                continue

            # Check if name contains query (case-insensitive)
            if query_lower in user.name.lower():
                results.append(UserSearchResult(user_id=user.user_id, email=user.email, name=user.name))
                seen_user_ids.add(user.user_id)

            # Also check if email contains query (for partial email matches)
            elif query_lower in user.email.lower():
                results.append(UserSearchResult(user_id=user.user_id, email=user.email, name=user.name))
                seen_user_ids.add(user.user_id)

            # Stop if we have enough results
            if len(results) >= limit:
                break

        # Sort results: exact email matches first, then by name
        results.sort(key=lambda x: (0 if query_lower == x.email.lower() else 1, x.name.lower()))  # Exact email match first  # Then by name

        # Limit results
        results = results[:limit]

        logger.info("User search completed")
        return UserSearchResponse(users=results)

    except Exception as e:
        logger.error("Error searching users", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to search users: {str(e)}")
