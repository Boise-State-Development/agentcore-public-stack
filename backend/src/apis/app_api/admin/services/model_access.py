"""Model Access Service

This service provides hybrid access checking for managed models,
supporting both the new AppRole system and legacy JWT role-based access.

During the transition period, access is granted if the user matches EITHER:
1. AppRole-based access (via allowed_app_roles)
2. Legacy JWT role-based access (via available_to_roles)

Once migration is complete, the legacy JWT role check can be removed.
"""

import logging
from typing import List, Optional

from apis.shared.auth.models import User
from apis.shared.rbac.service import AppRoleService, get_app_role_service
from apis.app_api.admin.models import ManagedModel

logger = logging.getLogger(__name__)


class ModelAccessService:
    """
    Service for checking user access to managed models.

    Supports hybrid access during the AppRole migration period:
    - Checks AppRole-based permissions first (preferred)
    - Falls back to legacy JWT role matching if no AppRoles configured
    """

    def __init__(self, app_role_service: Optional[AppRoleService] = None):
        """Initialize with optional AppRoleService dependency."""
        self._app_role_service = app_role_service

    @property
    def app_role_service(self) -> AppRoleService:
        """Lazy-load AppRoleService to avoid circular imports."""
        if self._app_role_service is None:
            self._app_role_service = get_app_role_service()
        return self._app_role_service

    async def can_access_model(self, user: User, model: ManagedModel) -> bool:
        """
        Check if a user can access a specific model.

        Access is granted if ANY of the following is true:
        1. Model has allowed_app_roles AND user has matching AppRole permissions
        2. Model has available_to_roles AND user has matching JWT role
        3. Neither field is set (model is unrestricted - should not happen in practice)

        Args:
            user: Authenticated user
            model: ManagedModel to check access for

        Returns:
            True if user can access the model, False otherwise
        """
        if not model.enabled:
            return False

        # Check AppRole-based access first (new system)
        if model.allowed_app_roles:
            try:
                permissions = await self.app_role_service.resolve_user_permissions(user)

                # Wildcard grants access to all models
                if "*" in permissions.models:
                    logger.debug(
                        f"User {user.email} has wildcard model access via AppRole"
                    )
                    return True

                # Check if user has access to this specific model
                if model.model_id in permissions.models:
                    logger.debug(
                        f"User {user.email} has AppRole access to model {model.model_id}"
                    )
                    return True

            except Exception as e:
                logger.warning(
                    f"Error checking AppRole permissions for {user.email}: {e}. "
                    "Falling back to JWT role check."
                )

        # Check legacy JWT role-based access (deprecated)
        if model.available_to_roles:
            user_roles = user.roles or []
            if any(role in model.available_to_roles for role in user_roles):
                logger.debug(
                    f"User {user.email} has legacy JWT role access to model {model.model_id}"
                )
                return True

        # No access configured or user doesn't match any access rules
        return False

    async def filter_accessible_models(
        self, user: User, models: List[ManagedModel]
    ) -> List[ManagedModel]:
        """
        Filter a list of models to only those the user can access.

        Args:
            user: Authenticated user
            models: List of ManagedModel objects to filter

        Returns:
            List of ManagedModel objects the user can access
        """
        accessible = []

        # Get user's AppRole permissions once (cached)
        try:
            permissions = await self.app_role_service.resolve_user_permissions(user)
            has_wildcard = "*" in permissions.models
            model_permissions = set(permissions.models)
        except Exception as e:
            logger.warning(
                f"Error resolving AppRole permissions for {user.email}: {e}. "
                "Using legacy JWT role check only."
            )
            permissions = None
            has_wildcard = False
            model_permissions = set()

        user_roles = set(user.roles or [])

        for model in models:
            if not model.enabled:
                continue

            # Check AppRole-based access (wildcard or specific model)
            if has_wildcard or model.model_id in model_permissions:
                accessible.append(model)
                continue

            # Check if model has allowed_app_roles and user matches
            if model.allowed_app_roles and permissions:
                # This model requires AppRole access, but user didn't have it
                # Still check legacy JWT roles as fallback
                pass

            # Check legacy JWT role-based access
            if model.available_to_roles:
                if user_roles.intersection(model.available_to_roles):
                    accessible.append(model)
                    continue

        logger.debug(
            f"Filtered {len(models)} models to {len(accessible)} accessible for {user.email}"
        )

        return accessible


# Global service instance (singleton)
_service_instance: Optional[ModelAccessService] = None


def get_model_access_service() -> ModelAccessService:
    """Get or create the global ModelAccessService instance."""
    global _service_instance
    if _service_instance is None:
        _service_instance = ModelAccessService()
    return _service_instance
