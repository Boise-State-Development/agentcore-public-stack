"""User sync service for JWT-to-DynamoDB synchronization."""

import logging
from datetime import datetime
from typing import Tuple, Optional

from .models import UserProfile, UserStatus
from .repository import UserRepository

logger = logging.getLogger(__name__)


class UserSyncService:
    """
    Syncs user data from JWT claims to DynamoDB.
    Called on each login/token validation to keep user data current.
    """

    def __init__(self, repository: UserRepository):
        """Initialize with user repository."""
        self._repository = repository

    @property
    def enabled(self) -> bool:
        """Check if sync is enabled (repository is configured)."""
        return self._repository.enabled

    async def sync_from_jwt(self, jwt_claims: dict) -> Tuple[Optional[UserProfile], bool]:
        """
        Create or update user from JWT claims.

        Args:
            jwt_claims: Decoded JWT payload containing user info.
                Expected claims:
                - sub: User ID
                - email: User email
                - name: Display name (optional)
                - roles: List of roles (optional)
                - picture: Profile picture URL (optional)

        Returns:
            Tuple of (UserProfile, is_new_user)
            Returns (None, False) if sync is disabled.
        """
        if not self.enabled:
            logger.debug("User sync disabled - no table configured")
            return None, False

        # Extract and validate required claims
        user_id = jwt_claims.get("sub")
        if not user_id:
            logger.warning("JWT missing 'sub' claim, skipping sync")
            return None, False

        email = jwt_claims.get("email", "")
        if not email:
            logger.warning(f"JWT missing 'email' claim for user {user_id}, skipping sync")
            return None, False

        # Extract domain from email
        email_domain = ""
        if "@" in email:
            email_domain = email.split("@")[1]

        now = datetime.utcnow().isoformat() + "Z"

        # Build profile from JWT claims
        profile = UserProfile(
            user_id=user_id,
            email=email.lower(),
            name=jwt_claims.get("name", ""),
            roles=jwt_claims.get("roles", []),
            picture=jwt_claims.get("picture"),
            email_domain=email_domain.lower(),
            created_at=now,  # Will be overwritten if user exists
            last_login_at=now,
            status=UserStatus.ACTIVE
        )

        try:
            # Upsert user
            profile, is_new = await self._repository.upsert_user(profile)

            if is_new:
                logger.info(f"Created new user from JWT: {user_id} ({email})")
            else:
                logger.debug(f"Updated user from JWT: {user_id}")

            return profile, is_new
        except Exception as e:
            logger.error(f"Error syncing user {user_id} from JWT: {e}")
            # Don't re-raise - sync failures shouldn't break the request
            return None, False

    async def sync_from_user(
        self,
        user_id: str,
        email: str,
        name: str = "",
        roles: list = None,
        picture: str = None
    ) -> Tuple[Optional[UserProfile], bool]:
        """
        Convenience method to sync from User model fields.

        Args:
            user_id: User's unique identifier
            email: User's email address
            name: User's display name
            roles: List of user roles
            picture: Profile picture URL

        Returns:
            Tuple of (UserProfile, is_new_user)
        """
        jwt_claims = {
            "sub": user_id,
            "email": email,
            "name": name,
            "roles": roles or [],
            "picture": picture
        }
        return await self.sync_from_jwt(jwt_claims)

    async def sync_user_from_jwt(self, user) -> Tuple[Optional[UserProfile], bool]:
        """
        Convenience method to sync from a User auth model.

        Args:
            user: User object from auth module (has user_id, email, name, roles, picture)

        Returns:
            Tuple of (UserProfile, is_new_user)
        """
        return await self.sync_from_user(
            user_id=user.user_id,
            email=user.email,
            name=user.name,
            roles=user.roles,
            picture=user.picture
        )
