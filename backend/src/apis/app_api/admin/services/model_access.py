"""Model Access Service

This service provides hybrid access checking for managed models,
supporting both the new AppRole system and legacy JWT role-based access.

During the transition period, access is granted if the user matches EITHER:
1. AppRole-based access — the model is in the user's resolved ``permissions.models``
   (i.e. some role of theirs lists it in ``grantedModels``, or grants ``*``)
2. Legacy JWT role-based access (via ``available_to_roles``)

Once migration is complete, the legacy JWT role check can be removed.

Note ``ManagedModel.allowed_app_roles`` is NOT an input to either check: it is a
field derived *from* the role records for display (see :mod:`.model_roles`).
Access is decided by the roles alone. Both entry points below funnel through
``_grants_access`` so a single-model check and a catalog filter can never
disagree about the same model.
"""

import logging
from typing import List, Optional, Set

from apis.shared.auth.models import User
from apis.shared.rbac.service import AppRoleService, get_app_role_service
from apis.shared.models.models import ManagedModel

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

    async def _resolve_model_permissions(self, user: User) -> Set[str]:
        """
        Resolve the set of model ids the user's AppRoles grant (may contain '*').

        Returns an empty set if permission resolution fails, so the caller falls
        through to the legacy JWT check rather than erroring.
        """
        try:
            permissions = await self.app_role_service.resolve_user_permissions(user)
            return set(permissions.models)
        except Exception as e:
            # JUSTIFICATION: AppRole permission resolution failures should not block access checks.
            # We fall back to legacy JWT role checking to maintain system availability during
            # the AppRole migration period. This ensures users can still access models even if
            # the AppRole system has issues. We log the error for monitoring.
            logger.warning(
                f"Error resolving AppRole permissions for {user.email} (falling back to JWT roles): {e}",
                exc_info=True
            )
            return set()

    @staticmethod
    def _grants_access(
        model: ManagedModel, model_permissions: Set[str], user_roles: Set[str]
    ) -> bool:
        """
        Decide whether a user may use a model. The single access rule.

        Both ``can_access_model`` and ``filter_accessible_models`` delegate here,
        so a model is never listed by one and denied by the other.
        """
        if not model.enabled:
            return False

        # AppRole-based access: a role of the user's grants this model (or all models).
        if "*" in model_permissions or model.model_id in model_permissions:
            return True

        # Legacy JWT role-based access (deprecated).
        if model.available_to_roles and user_roles.intersection(model.available_to_roles):
            return True

        return False

    async def can_access_model(self, user: User, model: ManagedModel) -> bool:
        """
        Check if a user can access a specific model.

        Args:
            user: Authenticated user
            model: ManagedModel to check access for

        Returns:
            True if user can access the model, False otherwise
        """
        # Short-circuit before paying for a permission resolve.
        if not model.enabled:
            return False

        model_permissions = await self._resolve_model_permissions(user)
        allowed = self._grants_access(model, model_permissions, set(user.roles or []))

        logger.debug(
            f"User {user.email} access to model {model.model_id}: {allowed}"
        )
        return allowed

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
        # Resolve the user's AppRole permissions once (cached) for the whole catalog.
        model_permissions = await self._resolve_model_permissions(user)
        user_roles = set(user.roles or [])

        accessible = [
            model
            for model in models
            if self._grants_access(model, model_permissions, user_roles)
        ]

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
