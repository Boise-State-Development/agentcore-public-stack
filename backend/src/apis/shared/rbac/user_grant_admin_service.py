"""Admin writes for direct user grants (``UserGrant``).

A direct grant hands tools, models and skills to one user beside their roles —
the exception path for one pilot user, one researcher's model, one dated
exception. Roles stay the normal way access is handed out; this never narrows
what a role gives (see the ``UserGrant`` docstring in ``.models``).

**Guarded by full admin, never a delegable scope.** Whoever can write a grant
can write one for themselves, which is role editing by another route — the
same reason ``admin.roles`` is non-delegable. The registry entry is
``admin.user_grants`` (``delegable=False``), and the routes in
``app_api/admin/user_grants/`` keep bare ``require_admin``.

**Validation is fail-closed against the catalogs.** The roles admin UI has no
free-text entry, so a role can only ever grant a catalog id; this surface is
reached by an API too, so it checks each id itself. An unknown id, a scoped
``server::tool`` ref (grants are by base id, like a role's), or ``"*"`` is a
400, not a silently inert row. Lookups go to the catalog repositories
directly rather than the per-process config cache: this is an admin write,
not the turn path, and a grant must not be refused because a tool created a
minute ago has not reached the cache.

**Every write invalidates the per-user permission cache in this process and
bumps the roles watermark.** Another process (the AgentCore Runtime) sees the
change at its own cache TTL, five minutes by default — the same propagation
a role edit has today.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, List, Optional, Sequence, Set

from apis.shared.audit import AuditService, get_audit_service
from apis.shared.audit.models import TARGET_USER_GRANT, AuditAction
from apis.shared.auth.models import User
from apis.shared.timestamps import utc_now_iso
from apis.shared.tools.scoped_ids import SCOPE_DELIMITER

from .cache import AppRoleCache, get_app_role_cache
from .models import UserGrant, UserGrantUpdate
from .repository import AppRoleRepository
from .version import bump_roles_version

logger = logging.getLogger(__name__)

#: Upper bound per list. Far above any real grant; exists so a runaway client
#: cannot write a row that approaches the DynamoDB item limit.
MAX_IDS_PER_LIST = 200

#: ``kind`` values the reverse lookup accepts.
GRANT_KINDS = ("tool", "model", "skill")

Exists = Callable[[str], Awaitable[bool]]


class UserGrantValidationError(ValueError):
    """The request is well-formed but names something that cannot be granted."""


class UnknownUserError(LookupError):
    """No user record exists for the id."""


async def _tool_exists(tool_id: str) -> bool:
    from apis.shared.tools.repository import get_tool_catalog_repository

    return await get_tool_catalog_repository().get_tool(tool_id) is not None


async def _model_exists(model_id: str) -> bool:
    from apis.shared.models.managed_models import find_managed_model_by_model_id

    return await find_managed_model_by_model_id(model_id) is not None


async def _skill_exists(skill_id: str) -> bool:
    from apis.shared.skills.repository import get_skill_catalog_repository

    return await get_skill_catalog_repository().get_skill(skill_id) is not None


async def _user_exists(user_id: str) -> bool:
    """Whether the users table knows ``user_id``.

    A deployment with no users table configured (local dev) cannot answer, so
    the check is skipped there rather than refusing every grant.
    """
    from apis.shared.users.repository import UserRepository

    repo = UserRepository()
    if not repo.enabled:
        return True
    return await repo.get_user(user_id) is not None


class UserGrantAdminService:
    """Read, replace and delete one user's direct grant."""

    AUDITED_FIELDS = (
        "granted_tools",
        "granted_models",
        "granted_skills",
        "expires_at",
        "note",
    )

    def __init__(
        self,
        repository: Optional[AppRoleRepository] = None,
        cache: Optional[AppRoleCache] = None,
        audit: Optional[AuditService] = None,
        *,
        tool_exists: Exists = _tool_exists,
        model_exists: Exists = _model_exists,
        skill_exists: Exists = _skill_exists,
        user_exists: Exists = _user_exists,
    ):
        self.repository = repository or AppRoleRepository()
        self.cache = cache or get_app_role_cache()
        self.audit = audit or get_audit_service()
        self._tool_exists = tool_exists
        self._model_exists = model_exists
        self._skill_exists = skill_exists
        self._user_exists = user_exists

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_grant(self, user_id: str) -> Optional[UserGrant]:
        return await self.repository.get_user_grant(user_id)

    async def list_grants(self) -> List[UserGrant]:
        grants = await self.repository.list_user_grants()
        return sorted(grants, key=lambda g: g.user_id)

    async def holders(self, kind: str, resource_id: str) -> List[str]:
        """User ids directly granted one resource."""
        if kind == "tool":
            return await self.repository.get_user_ids_for_tool(resource_id)
        if kind == "model":
            return await self.repository.get_user_ids_for_model(resource_id)
        if kind == "skill":
            return await self.repository.get_user_ids_for_skill(resource_id)
        raise UserGrantValidationError(
            f"Unknown grant kind '{kind}'. Expected one of: {', '.join(GRANT_KINDS)}."
        )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def set_grant(
        self, user_id: str, update: UserGrantUpdate, admin: User
    ) -> Optional[UserGrant]:
        """Replace ``user_id``'s grant with ``update``.

        A request that grants nothing removes the row instead of storing an
        empty one, so "no direct grant" has exactly one representation and
        the list of grant holders stays a list of people who hold something.
        Returns the stored grant, or ``None`` when the row was removed.
        """
        if not await self._user_exists(user_id):
            raise UnknownUserError(f"User {user_id} not found")

        grant = UserGrant(
            user_id=user_id,
            granted_tools=await self._validated_ids(
                update.granted_tools, "tool", self._tool_exists
            ),
            granted_models=await self._validated_ids(
                update.granted_models, "model", self._model_exists
            ),
            granted_skills=await self._validated_ids(
                update.granted_skills, "skill", self._skill_exists
            ),
            expires_at=self._validated_expiry(update.expires_at),
            note=(update.note or "").strip(),
            granted_by=admin.email,
        )

        before = await self.repository.get_user_grant(user_id)

        if grant.is_empty():
            if before is None:
                return None
            await self.delete_grant(user_id, admin)
            return None

        stored = await self.repository.put_user_grant(grant)
        await self._invalidate(user_id)

        changed, before_values, after_values = self._diff(before, stored)
        logger.info(
            f"Admin {admin.email} set direct grant for user {user_id}",
            extra={
                "event": "user_grant_updated",
                "target_user_id": user_id,
                "admin_user_id": admin.user_id,
                "admin_email": admin.email,
                "changes": changed,
            },
        )
        if changed:
            self.audit.record(
                action=AuditAction.USER_GRANT_UPDATED,
                actor=admin,
                target_type=TARGET_USER_GRANT,
                target_id=user_id,
                changes=changed,
                before=before_values,
                after=after_values,
            )
        return stored

    async def delete_grant(self, user_id: str, admin: User) -> bool:
        """Remove ``user_id``'s grant. Returns False when there was none."""
        before = await self.repository.get_user_grant(user_id)
        if before is None:
            return False

        deleted = await self.repository.delete_user_grant(user_id)
        if not deleted:
            return False

        await self._invalidate(user_id)
        logger.info(
            f"Admin {admin.email} deleted direct grant for user {user_id}",
            extra={
                "event": "user_grant_deleted",
                "target_user_id": user_id,
                "admin_user_id": admin.user_id,
                "admin_email": admin.email,
            },
        )
        _, before_values, _ = self._diff(before, None)
        self.audit.record(
            action=AuditAction.USER_GRANT_DELETED,
            actor=admin,
            target_type=TARGET_USER_GRANT,
            target_id=user_id,
            changes=list(self.AUDITED_FIELDS),
            before=before_values,
            after={},
        )
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _invalidate(self, user_id: str) -> None:
        await self.cache.invalidate_user(user_id)
        bump_roles_version()

    async def _validated_ids(
        self, ids: Sequence[str], kind: str, exists: Exists
    ) -> List[str]:
        """Dedupe, sort and check every id against its catalog."""
        cleaned: Set[str] = set()
        for raw in ids or []:
            if not isinstance(raw, str) or not raw.strip():
                raise UserGrantValidationError(f"Each {kind} id must be a non-empty string.")
            value = raw.strip()
            if value == "*":
                raise UserGrantValidationError(
                    "A direct grant cannot hold '*'. Assign a role for wildcard access."
                )
            if kind == "tool" and SCOPE_DELIMITER in value:
                raise UserGrantValidationError(
                    f"Grant the whole server, not a scoped tool: '{value}'."
                )
            cleaned.add(value)

        if len(cleaned) > MAX_IDS_PER_LIST:
            raise UserGrantValidationError(
                f"At most {MAX_IDS_PER_LIST} {kind} ids per grant."
            )

        unknown = []
        for value in sorted(cleaned):
            if not await exists(value):
                unknown.append(value)
        if unknown:
            raise UserGrantValidationError(
                f"Unknown {kind} id(s): {', '.join(unknown)}."
            )
        return sorted(cleaned)

    @staticmethod
    def _validated_expiry(value: Optional[str]) -> Optional[str]:
        """Normalise ``expires_at`` to the canonical UTC spelling, or refuse it."""
        if value is None or not str(value).strip():
            return None
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise UserGrantValidationError(
                "expiresAt must be an ISO 8601 timestamp."
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        if parsed <= datetime.now(timezone.utc):
            raise UserGrantValidationError("expiresAt must be in the future.")
        return parsed.isoformat()

    def _diff(
        self, before: Optional[UserGrant], after: Optional[UserGrant]
    ) -> tuple[List[str], dict, dict]:
        changed: List[str] = []
        before_values: dict = {}
        after_values: dict = {}
        for name in self.AUDITED_FIELDS:
            b = getattr(before, name, None) if before else None
            a = getattr(after, name, None) if after else None
            if b != a:
                changed.append(name)
                before_values[name] = b
                after_values[name] = a
        return changed, before_values, after_values


_service_instance: Optional[UserGrantAdminService] = None


def get_user_grant_admin_service() -> UserGrantAdminService:
    """Get or create the global service instance."""
    global _service_instance
    if _service_instance is None:
        _service_instance = UserGrantAdminService()
    return _service_instance
