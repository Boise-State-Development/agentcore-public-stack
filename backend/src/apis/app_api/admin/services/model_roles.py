"""Model Role Service

Keeps model↔role permissions in one place: the AppRole record.

A role grants a model by listing it in its own ``grantedModels`` (or by granting
the ``*`` wildcard, or by inheriting from a parent that does). That is the only
thing the access checks in :mod:`.model_access` ever read.

The admin model form still presents a "which roles can use this model?" picker.
This service is what makes that picker honest:

* :meth:`set_roles_for_model` writes the picker's selection *through* to each
  role's ``grantedModels`` — the same shape as ``set_roles_for_tool`` in
  ``app_api/tools/service.py``.
* :meth:`hydrate_model_roles` recomputes ``allowed_app_roles`` /
  ``inherited_app_roles`` from the role records on read, so the model record
  never stores a role list that can drift out of date.

Historically ``allowedAppRoles`` was a stored, client-writable field that no
access check consulted, so editing it on the model page silently did nothing.
"""

import logging
from typing import Dict, List, Optional, Set

from apis.shared.auth.models import User
from apis.shared.models.models import ManagedModel, ModelRoleAssignment
from apis.shared.rbac.admin_service import (
    AppRoleAdminService,
    get_app_role_admin_service,
)
from apis.shared.rbac.models import AppRole, AppRoleUpdate

logger = logging.getLogger(__name__)

WILDCARD = "*"


class ModelRoleService:
    """Reads and writes model grants against the AppRole records."""

    def __init__(self, app_role_admin_service: Optional[AppRoleAdminService] = None):
        self._admin_service = app_role_admin_service

    @property
    def admin_service(self) -> AppRoleAdminService:
        """Lazy-load AppRoleAdminService to avoid circular imports."""
        if self._admin_service is None:
            self._admin_service = get_app_role_admin_service()
        return self._admin_service

    # =========================================================================
    # Read — derive a model's roles from the role records
    # =========================================================================

    async def get_roles_for_model(self, model_id: str) -> List[ModelRoleAssignment]:
        """
        Get every AppRole that grants access to a model.

        Args:
            model_id: The *provider* model id (e.g. ``anthropic.claude-...``),
                which is what roles store in ``grantedModels`` — not the model
                record's internal UUID.

        Returns:
            One assignment per granting role, tagged direct / wildcard / inherited.
        """
        roles = await self.admin_service.list_roles(enabled_only=False)
        return self._assignments_for(model_id, roles)

    async def hydrate_model_roles(self, models: List[ManagedModel]) -> List[ManagedModel]:
        """
        Populate the derived ``allowed_app_roles`` / ``inherited_app_roles`` on
        each model from the role records.

        Loads the role list once and resolves every model against it in memory,
        so hydrating a full catalog costs a single role query.

        Args:
            models: Models to hydrate (mutated in place and returned).

        Returns:
            The same list, with derived role fields populated.
        """
        if not models:
            return models

        roles = await self.admin_service.list_roles(enabled_only=False)

        for model in models:
            assignments = self._assignments_for(model.model_id, roles)
            model.allowed_app_roles = [
                a.role_id for a in assignments if a.grant_type == "direct"
            ]
            model.inherited_app_roles = [
                a.role_id for a in assignments if a.grant_type != "direct"
            ]

        return models

    def _assignments_for(
        self, model_id: str, roles: List[AppRole]
    ) -> List[ModelRoleAssignment]:
        """Classify how (if at all) each role grants ``model_id``."""
        by_id: Dict[str, AppRole] = {r.role_id: r for r in roles}
        assignments: List[ModelRoleAssignment] = []

        for role in roles:
            granted = set(role.granted_models)

            if model_id in granted:
                grant_type, inherited_from = "direct", None
            elif WILDCARD in granted:
                grant_type, inherited_from = "wildcard", None
            else:
                # Not granted directly — see whether a parent supplies it.
                inherited_from = self._parent_granting(model_id, role, by_id)
                if inherited_from is None:
                    continue
                grant_type = "inherited"

            assignments.append(
                ModelRoleAssignment(
                    role_id=role.role_id,
                    display_name=role.display_name,
                    grant_type=grant_type,
                    inherited_from=inherited_from,
                    enabled=role.enabled,
                )
            )

        return assignments

    @staticmethod
    def _parent_granting(
        model_id: str, role: AppRole, by_id: Dict[str, AppRole]
    ) -> Optional[str]:
        """Return the id of the first enabled parent role granting the model."""
        for parent_id in role.inherits_from:
            parent = by_id.get(parent_id)
            if not parent or not parent.enabled:
                continue
            parent_grants = set(parent.granted_models)
            if model_id in parent_grants or WILDCARD in parent_grants:
                return parent_id
        return None

    # =========================================================================
    # Write — push the model form's role picker into the role records
    # =========================================================================

    async def set_roles_for_model(
        self,
        model_id: str,
        app_role_ids: List[str],
        admin: User,
        previous_model_id: Optional[str] = None,
    ) -> None:
        """
        Make exactly ``app_role_ids`` grant this model directly.

        Adds the model to each named role's ``grantedModels`` and removes it from
        any role that grants it directly but isn't named. Roles that grant the
        model only via wildcard or inheritance are left alone — they aren't
        "direct" grants, so they're neither added to nor stripped by this call.

        Args:
            model_id: The provider model id roles store in ``grantedModels``.
            app_role_ids: Roles that should grant the model directly.
            admin: Admin performing the change (for the audit log).
            previous_model_id: Set when an update renamed the model id, so grants
                pointing at the old id are migrated rather than orphaned.

        Raises:
            ValueError: If any named role does not exist.
        """
        roles = await self.admin_service.list_roles(enabled_only=False)
        by_id: Dict[str, AppRole] = {r.role_id: r for r in roles}

        requested: Set[str] = set(app_role_ids)
        unknown = requested - by_id.keys()
        if unknown:
            raise ValueError(f"Unknown AppRole(s): {sorted(unknown)}")

        # A rename leaves grants pointing at the old id; drop them so the role's
        # grantedModels doesn't accumulate a dangling entry.
        stale_id = (
            previous_model_id
            if previous_model_id and previous_model_id != model_id
            else None
        )

        for role in roles:
            granted = list(role.granted_models)
            updated = [m for m in granted if m != stale_id] if stale_id else list(granted)

            should_grant = role.role_id in requested
            grants_directly = model_id in updated

            if should_grant and not grants_directly:
                # A wildcard role already covers every model; adding the explicit
                # id would be redundant noise in its grantedModels.
                if WILDCARD not in updated:
                    updated.append(model_id)
            elif not should_grant and grants_directly:
                updated = [m for m in updated if m != model_id]

            if updated != granted:
                await self.admin_service.update_role(
                    role.role_id, AppRoleUpdate(granted_models=updated), admin
                )

        logger.info(
            f"Admin {admin.email} set roles for model {model_id}",
            extra={
                "event": "model_roles_updated",
                "model_id": model_id,
                "admin_user_id": admin.user_id,
                "roles": sorted(requested),
            },
        )

    async def revoke_model_from_all_roles(self, model_id: str, admin: User) -> None:
        """
        Strip a model from every role's ``grantedModels``.

        Called when a model is deleted so roles don't retain grants for a model
        that no longer exists.
        """
        roles = await self.admin_service.list_roles(enabled_only=False)

        for role in roles:
            if model_id not in role.granted_models:
                continue
            remaining = [m for m in role.granted_models if m != model_id]
            await self.admin_service.update_role(
                role.role_id, AppRoleUpdate(granted_models=remaining), admin
            )

        logger.info(
            f"Admin {admin.email} revoked model {model_id} from all roles",
            extra={
                "event": "model_roles_revoked",
                "model_id": model_id,
                "admin_user_id": admin.user_id,
            },
        )


# Global service instance
_service_instance: Optional[ModelRoleService] = None


def get_model_role_service() -> ModelRoleService:
    """Get or create the global ModelRoleService instance."""
    global _service_instance
    if _service_instance is None:
        _service_instance = ModelRoleService()
    return _service_instance
