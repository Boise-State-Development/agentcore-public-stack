"""User-authored skills service (Skills v2 PR-3).

The owner-scoped half of the skill catalog. Admin catalog skills
(``owner_id == "system"``, governed by RBAC ``granted_skills``) and
user-authored skills (``owner_id == <user_id>``, governed by ownership) are
the same record type in the same table — two authorship tiers, one store
(spec D8). This service is the ownership-governed entry point; every method
resolves the record and refuses to touch one the caller does not own.

Resource-file handling (caps, manifest, S3 bundle layout, orphan GC) is
*not* reimplemented here — it is delegated to :class:`SkillCatalogService`
after the ownership check, so both tiers write identical agentskills.io
bundles through one code path.
"""

import logging
import re
from typing import Dict, List, Optional, Tuple

from apis.shared.auth.models import User
from apis.shared.skills.models import (
    SKILL_ID_PATTERN,
    SkillDefinition,
    SkillResourceRef,
    SkillStatus,
    SkillVisibility,
)
from apis.shared.skills.repository import (
    SkillCatalogRepository,
    get_skill_catalog_repository,
)

from .service import SkillCatalogService, get_skill_catalog_service

logger = logging.getLogger(__name__)

# A user may author this many skills. Generous enough that nobody legitimate
# hits it; low enough that one account cannot inflate the shared catalog table
# (every admin catalog list still scans over these rows).
MAX_SKILLS_PER_USER = 50

# Fallback stem when a display name slugifies to nothing usable (e.g. a name
# written entirely in a non-Latin script).
_FALLBACK_STEM = "skill"
_SKILL_ID_RE = re.compile(SKILL_ID_PATTERN)


class UserSkillError(Exception):
    """Base class for user-tier skill failures (400)."""


class UserSkillNotFoundError(UserSkillError):
    """The skill does not exist, or is not visible to this caller (404)."""


class UserSkillLimitError(UserSkillError):
    """The caller is at their authored-skill cap (409)."""


def slugify_skill_id(display_name: str) -> str:
    """Derive a ``skill_id`` stem from a human display name.

    Produces the ``SKILL_ID_PATTERN`` shape: lowercase, underscore-separated,
    leading letter, 3-50 chars. This is the *stem* only — uniqueness is
    settled by :meth:`UserSkillService._allocate_skill_id`.
    """
    stem = re.sub(r"[^a-z0-9]+", "_", (display_name or "").lower()).strip("_")
    # The pattern demands a leading letter, so a name like "3d modeling" needs
    # a prefix rather than a truncation that would change its meaning.
    if not stem or not stem[0].isalpha():
        stem = f"{_FALLBACK_STEM}_{stem}".strip("_") if stem else _FALLBACK_STEM
    # Leave room for a disambiguating suffix (see _allocate_skill_id).
    stem = stem[:45].rstrip("_")
    while len(stem) < 3:
        stem = f"{stem}_x" if stem else _FALLBACK_STEM
    return stem


class UserSkillService:
    """Owner-scoped CRUD over the user-authored skill tier."""

    def __init__(
        self,
        repository: Optional[SkillCatalogRepository] = None,
        catalog_service: Optional[SkillCatalogService] = None,
    ):
        self.repository = repository or get_skill_catalog_repository()
        # Reused purely for its resource-file machinery (caps, manifest,
        # SKILL.md projection, orphan GC) — never for its role-sync methods,
        # which are admin-only by construction.
        self.catalog_service = catalog_service or get_skill_catalog_service()

    # =========================================================================
    # Ownership
    # =========================================================================

    async def require_owned(self, skill_id: str, user: User) -> SkillDefinition:
        """Load a skill and assert the caller authored it.

        Raises:
            UserSkillNotFoundError: No such skill, or it belongs to the admin
                catalog / another user. Both collapse to "not found" so this
                surface never confirms the existence of someone else's skill.
        """
        skill = await self.repository.get_skill(skill_id)
        if skill is None or skill.owner_id != user.user_id:
            raise UserSkillNotFoundError(f"Skill '{skill_id}' not found")
        return skill

    # =========================================================================
    # CRUD
    # =========================================================================

    async def list_my_skills(self, user: User) -> List[SkillDefinition]:
        """Every skill the caller authored, any status (GSI4 partition query)."""
        return await self.repository.list_skills_by_owner(user.user_id)

    async def get_my_skill(self, skill_id: str, user: User) -> SkillDefinition:
        """One owned skill, including its resource manifest."""
        return await self.require_owned(skill_id, user)

    async def create_my_skill(
        self,
        user: User,
        *,
        display_name: str,
        description: str,
        instructions: str = "",
        allowed_tools: Optional[List[str]] = None,
        skill_metadata: Optional[Dict] = None,
        category: Optional[str] = None,
    ) -> SkillDefinition:
        """Create a skill owned by ``user``.

        The ``skill_id`` is allocated server-side from the display name — users
        never type one. Ids are globally unique across both tiers on purpose:
        the runtime activation key is the (slugified) id, so two same-named
        skills in one turn would be ambiguous to the model.

        Raises:
            UserSkillLimitError: Caller is at ``MAX_SKILLS_PER_USER``.
            UserSkillError: Display name or description is blank.
        """
        display_name = (display_name or "").strip()
        description = (description or "").strip()
        if not display_name:
            raise UserSkillError("A skill needs a name.")
        if not description:
            raise UserSkillError(
                "A skill needs a description — it is the one line the agent "
                "reads when deciding whether to use the skill."
            )

        existing = await self.repository.list_skills_by_owner(user.user_id)
        if len(existing) >= MAX_SKILLS_PER_USER:
            raise UserSkillLimitError(
                f"You already have the maximum of {MAX_SKILLS_PER_USER} skills. "
                "Delete one before creating another."
            )

        skill_id = await self._allocate_skill_id(display_name)
        skill = SkillDefinition(
            skill_id=skill_id,
            display_name=display_name,
            description=description,
            instructions=instructions or "",
            allowed_tools=list(allowed_tools or []),
            skill_metadata=dict(skill_metadata or {}),
            category=category,
            status=SkillStatus.ACTIVE,
            owner_id=user.user_id,
            visibility=SkillVisibility.PRIVATE,
            created_by=user.user_id,
            updated_by=user.user_id,
        )

        created = await self.repository.create_skill(skill)
        self.catalog_service.write_skill_md(created)
        self._invalidate(skill_id)

        logger.info(
            f"User {user.email} created skill: {skill_id}",
            extra={
                "event": "user_skill_created",
                "skill_id": skill_id,
                "owner_user_id": user.user_id,
            },
        )
        return created

    async def update_my_skill(
        self, skill_id: str, updates: Dict, user: User
    ) -> SkillDefinition:
        """Update an owned skill's authored fields.

        ``updates`` is already narrowed to the caller-writable fields by the
        route DTO — ``owner_id``, ``visibility`` and audit fields are never
        routed through here.
        """
        await self.require_owned(skill_id, user)

        updated = await self.repository.update_skill(
            skill_id, updates, admin_user_id=user.user_id
        )
        if updated is None:
            raise UserSkillNotFoundError(f"Skill '{skill_id}' not found")

        self.catalog_service.write_skill_md(updated)
        self._invalidate(skill_id)

        logger.info(
            f"User {user.email} updated skill: {skill_id}",
            extra={
                "event": "user_skill_updated",
                "skill_id": skill_id,
                "owner_user_id": user.user_id,
                "changes": list(updates.keys()),
            },
        )
        return updated

    async def delete_my_skill(self, skill_id: str, user: User) -> None:
        """Hard-delete an owned skill and purge its bundle objects.

        Deliberately a *hard* delete, unlike the admin catalog's default soft
        delete: an admin skill may be referenced by role grants worth auditing,
        whereas a user deleting their own draft expects it gone. Bundle objects
        are removed first so a failed row delete cannot strand a live skill
        pointing at missing files.
        """
        skill = await self.require_owned(skill_id, user)

        for ref in skill.resources:
            self.catalog_service.resource_store.delete(ref.s3_key)
        self._delete_skill_md(skill_id)

        await self.repository.delete_skill(skill_id)
        self._invalidate(skill_id)

        logger.info(
            f"User {user.email} deleted skill: {skill_id}",
            extra={
                "event": "user_skill_deleted",
                "skill_id": skill_id,
                "owner_user_id": user.user_id,
            },
        )

    # =========================================================================
    # Bundle files — delegated to the shared catalog service post-ownership
    # =========================================================================

    async def list_resources(
        self, skill_id: str, user: User
    ) -> List[SkillResourceRef]:
        skill = await self.require_owned(skill_id, user)
        return list(skill.resources)

    async def add_resource(
        self,
        skill_id: str,
        filename: str,
        content: bytes,
        content_type: str,
        user: User,
        kind: str = "reference",
    ) -> List[SkillResourceRef]:
        await self.require_owned(skill_id, user)
        refs = await self.catalog_service.add_resource(
            skill_id, filename, content, content_type, user, kind=kind
        )
        self._invalidate(skill_id)
        return refs

    async def read_resource(
        self, skill_id: str, filename: str, user: User
    ) -> Tuple[SkillResourceRef, bytes]:
        await self.require_owned(skill_id, user)
        return await self.catalog_service.read_resource(skill_id, filename)

    async def delete_resource(
        self, skill_id: str, filename: str, user: User
    ) -> List[SkillResourceRef]:
        await self.require_owned(skill_id, user)
        refs = await self.catalog_service.delete_resource(skill_id, filename, user)
        self._invalidate(skill_id)
        return refs

    # =========================================================================
    # Internals
    # =========================================================================

    async def _allocate_skill_id(self, display_name: str) -> str:
        """Pick a globally-unique ``skill_id`` from a display-name stem.

        Collisions are resolved by suffixing rather than rejected, so a user
        naming their skill "docx" when the catalog already has one succeeds
        (as ``docx_2``) instead of hitting a 409 that would also disclose the
        existence of a skill they cannot see.
        """
        stem = slugify_skill_id(display_name)
        candidate = stem
        for suffix in range(2, 100):
            if not await self.repository.skill_exists(candidate):
                return candidate
            candidate = f"{stem}_{suffix}"

        # Astronomically unlikely; fall back to a random tail rather than loop.
        import uuid

        candidate = f"{stem}_{uuid.uuid4().hex[:6]}"
        if not _SKILL_ID_RE.match(candidate):
            candidate = f"{_FALLBACK_STEM}_{uuid.uuid4().hex[:8]}"
        return candidate

    def _delete_skill_md(self, skill_id: str) -> None:
        """Best-effort removal of the SKILL.md projection for a deleted skill."""
        from apis.shared.skills.resource_store import skill_md_key

        self.catalog_service.resource_store.delete(skill_md_key(skill_id))

    @staticmethod
    def _invalidate(skill_id: str) -> None:
        """Drop the freshness caches so the change is visible on the next turn."""
        from apis.shared.skills.freshness import invalidate

        invalidate(skill_id)


_user_service_instance: Optional[UserSkillService] = None


def get_user_skill_service() -> UserSkillService:
    """Get or create the global UserSkillService instance."""
    global _user_service_instance
    if _user_service_instance is None:
        _user_service_instance = UserSkillService()
    return _user_service_instance
