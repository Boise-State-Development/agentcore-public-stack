"""User-authored skills service (Skills v2 PR-3; moved to ``apis.shared`` by
skill-creator spec D5 so the runtime's authoring tools can use it).

The owner-scoped half of the skill catalog. Admin catalog skills
(``owner_id == "system"``, governed by RBAC ``granted_skills``) and
user-authored skills (``owner_id == <user_id>``, governed by ownership) are
the same record type in the same table — two authorship tiers, one store
(spec D8). This service is the ownership-governed entry point; every method
resolves the record and refuses to touch one the caller does not own.

Resource-file handling (caps, manifest, S3 bundle layout, orphan GC) is
*not* reimplemented here — it is delegated to :class:`SkillBundleFiles`
after the ownership check, so both tiers write identical agentskills.io
bundles through one code path.
"""

import logging
import re
from typing import Dict, List, Optional, Tuple

from apis.shared.audit.models import AuditAction
from apis.shared.auth.models import User
from apis.shared.security.log_sanitize import scrub_log
from apis.shared.skills import history
from apis.shared.skills.bundle_files import SkillBundleFiles, get_skill_bundle_files
from apis.shared.skills.audit import is_no_op, record_skill_event
from apis.shared.skills.bundle_import import ParsedBundle
from apis.shared.skills.models import (
    SKILL_ID_PATTERN,
    SkillDefinition,
    SkillImportSource,
    SkillResourceRef,
    SkillStatus,
    SkillVisibility,
)
from apis.shared.skills.repository import (
    SkillCatalogRepository,
    get_skill_catalog_repository,
)
from apis.shared.skills.resource_types import (
    is_text_content_type,
    resolve_upload_content_type,
)
from apis.shared.skills.skill_md import validate_frontmatter
from apis.shared.skills.versions import SNAPSHOT_FIELDS

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


class UserSkillConflictError(UserSkillError):
    """An import's source or target moved since its preview (409)."""


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


def skill_id_from_frontmatter_name(name: object) -> Optional[str]:
    """The ``skill_id`` stem an imported ``name`` maps to, or None if it cannot be one.

    ``mcp-builder`` → ``mcp_builder``. The name must pass the agentskills.io
    name rule, and the converted form must fit ``SKILL_ID_PATTERN`` (so ``ab``,
    a name starting with a digit, and one over 50 characters fall back to the
    display name). Allocating from the name is what lets an import export back
    under the same ``name`` (it reverses a detail of skill-bundle-import.md §4,
    by decision).
    """
    if not isinstance(name, str):
        return None
    if any(d.field == "name" for d in validate_frontmatter({"name": name}, strict=False)):
        return None
    candidate = name.replace("-", "_")
    return candidate if _SKILL_ID_RE.match(candidate) else None


def bundle_original_name(bundle: ParsedBundle) -> Optional[str]:
    """The frontmatter ``name`` exactly as the bundle wrote it, if it wrote one."""
    name = bundle.frontmatter.get("name")
    return name if isinstance(name, str) and name else None


class UserSkillService:
    """Owner-scoped CRUD over the user-authored skill tier."""

    def __init__(
        self,
        repository: Optional[SkillCatalogRepository] = None,
        bundle_files: Optional[SkillBundleFiles] = None,
    ):
        self.repository = repository or get_skill_catalog_repository()
        # The bundle layout both tiers share (caps, manifest, SKILL.md
        # projection, orphan GC). It makes no access decision; every call
        # below checks ownership first.
        self.bundle_files = bundle_files or get_skill_bundle_files()

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
        kind: Optional[str] = None,
        trigger: history.VersionTrigger = "create",
    ) -> SkillDefinition:
        """Create a skill owned by ``user``.

        ``kind="workflow"`` marks a skill the workflow designer creates to carry a
        saved workflow (workflow-designer §20.3); every other caller passes none.

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
            kind=kind,
            status=SkillStatus.ACTIVE,
            owner_id=user.user_id,
            visibility=SkillVisibility.PRIVATE,
            created_by=user.user_id,
            updated_by=user.user_id,
        )

        created = await self.repository.create_skill(skill)
        self.bundle_files.write_skill_md(created)
        self._invalidate(skill_id)
        await self._capture(skill_id, user, trigger, live=created)
        await record_skill_event(AuditAction.SKILL_CREATED, user, skill_id, after=created)

        logger.info(
            f"User {scrub_log(user.email)} created skill: {scrub_log(skill_id)}",
            extra={
                "event": "user_skill_created",
                "skill_id": scrub_log(skill_id),
                "owner_user_id": user.user_id,
            },
        )
        return created

    async def update_my_skill(
        self,
        skill_id: str,
        updates: Dict,
        user: User,
        *,
        trigger: history.VersionTrigger = "save",
    ) -> SkillDefinition:
        """Update an owned skill's authored fields.

        ``updates`` is already narrowed to the caller-writable fields by the
        route DTO — ``owner_id``, ``visibility`` and audit fields are never
        routed through here.
        """
        before = await self.require_owned(skill_id, user)
        touches_content = bool(set(updates) & set(SNAPSHOT_FIELDS))
        if touches_content:
            await self._checkpoint(skill_id, user, live=before)

        updated = await self.repository.update_skill(
            skill_id, updates, admin_user_id=user.user_id
        )
        if updated is None:
            raise UserSkillNotFoundError(f"Skill '{skill_id}' not found")

        self.bundle_files.write_skill_md(updated)
        self._invalidate(skill_id)
        if touches_content:
            await self._capture(skill_id, user, trigger, live=updated)
        if not is_no_op(before, updated):
            await record_skill_event(
                AuditAction.SKILL_UPDATED, user, skill_id, before=before, after=updated
            )

        logger.info(
            f"User {scrub_log(user.email)} updated skill: {scrub_log(skill_id)}",
            extra={
                "event": "user_skill_updated",
                "skill_id": scrub_log(skill_id),
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
            self.bundle_files.resource_store.delete(ref.s3_key)
        self.bundle_files.delete_skill_md(skill_id)

        await self.repository.delete_skill(skill_id)
        await self.bundle_files.delete_versions(skill_id)
        self._invalidate(skill_id)
        await record_skill_event(AuditAction.SKILL_DELETED, user, skill_id, before=skill)

        logger.info(
            f"User {scrub_log(user.email)} deleted skill: {scrub_log(skill_id)}",
            extra={
                "event": "user_skill_deleted",
                "skill_id": scrub_log(skill_id),
                "owner_user_id": user.user_id,
            },
        )

    # =========================================================================
    # Bundle files — delegated to SkillBundleFiles post-ownership
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
        path: Optional[str] = None,
        *,
        trigger: history.VersionTrigger = "file",
    ) -> List[SkillResourceRef]:
        before = await self.require_owned(skill_id, user)
        # Before the write: re-uploading a filename overwrites its live key in place.
        await self._checkpoint(skill_id, user, live=before)
        refs = await self.bundle_files.add_resource(
            skill_id, filename, content, content_type, user, kind=kind, path=path
        )
        self._invalidate(skill_id)
        await self._capture(skill_id, user, trigger)
        await self._record_files_changed(user, before, refs)
        return refs

    async def read_resource(
        self, skill_id: str, filename: str, user: User
    ) -> Tuple[SkillResourceRef, bytes]:
        await self.require_owned(skill_id, user)
        return await self.bundle_files.read_resource(skill_id, filename)

    async def delete_resource(
        self,
        skill_id: str,
        filename: str,
        user: User,
        *,
        trigger: history.VersionTrigger = "file",
    ) -> List[SkillResourceRef]:
        before = await self.require_owned(skill_id, user)
        await self._checkpoint(skill_id, user, live=before)
        refs = await self.bundle_files.delete_resource(skill_id, filename, user)
        self._invalidate(skill_id)
        await self._capture(skill_id, user, trigger)
        await self._record_files_changed(user, before, refs)
        return refs

    @staticmethod
    async def _record_files_changed(
        user: User, before: SkillDefinition, refs: List[SkillResourceRef]
    ) -> None:
        """One ``skill.updated`` for a file write, none when the bytes were the same."""
        after = before.model_copy(update={"resources": list(refs)})
        if not is_no_op(before, after):
            await record_skill_event(
                AuditAction.SKILL_UPDATED, user, before.skill_id, before=before, after=after
            )

    async def write_text_file(
        self,
        skill_id: str,
        kind: str,
        filename: str,
        content: str,
        user: User,
        *,
        trigger: history.VersionTrigger = "file",
    ) -> List[SkillResourceRef]:
        """Write one bundle file from inline text (skill-creator spec D5).

        The agent's entry point: it produces text, not a multipart upload. The
        text is UTF-8 encoded and goes through :meth:`add_resource`, so the
        caps, the type allowlist, the manifest and orphan GC are the upload
        route's, and the bundle is laid out exactly as a hand upload would be.

        Only a filename whose allowlisted type is text is accepted. The
        allowlist also admits ``.png`` or ``.pdf`` for uploads, but text
        written under one of those names is a corrupt binary that every reader
        would then mis-handle.

        Raises:
            UserSkillNotFoundError: Not the caller's skill.
            ValueError: The kind, filename or size is invalid, the type is not
                allowed or not text, or the per-skill file cap is reached.
        """
        await self.require_owned(skill_id, user)
        content_type = resolve_upload_content_type(filename)
        if not is_text_content_type(content_type):
            raise ValueError(
                f"'{filename}' is a {content_type} file, which cannot be written "
                "as text. Use a text format such as .md, .txt, .json, .yaml or "
                "a source-code extension."
            )
        return await self.add_resource(
            skill_id,
            filename,
            content.encode("utf-8"),
            content_type,
            user,
            kind=kind,
            trigger=trigger,
        )

    # =========================================================================
    # Bundle import (docs/specs/skill-bundle-import.md)
    # =========================================================================

    async def import_bundle(
        self,
        user: User,
        bundle: ParsedBundle,
        *,
        source: SkillImportSource,
        skill_id: Optional[str] = None,
        display_name: Optional[str] = None,
        expected_bundle_hash: Optional[str] = None,
        expected_updated_at: Optional[str] = None,
    ) -> SkillDefinition:
        """Create a skill from ``bundle``, or re-import it into an owned skill.

        A re-import replaces the description, instructions, advisory tools and
        frontmatter, writes added and changed files, and drops files the bundle
        no longer has (spec D4). The display name stays the user's. It writes the
        **live row only**: no version is cut, so a project pinned to a version
        keeps it until someone clicks Update (D5).

        Raises:
            UserSkillConflictError: ``expected_bundle_hash`` or
                ``expected_updated_at`` no longer match (D3).
            UserSkillNotFoundError: ``skill_id`` is not the caller's.
            UserSkillLimitError: Creating would pass ``MAX_SKILLS_PER_USER``.
            ValueError: A file breaks a bundle rule.
        """
        if expected_bundle_hash and expected_bundle_hash != bundle.bundle_hash:
            raise UserSkillConflictError(
                "The source changed since you previewed it. Review the import again."
            )
        source = source.model_copy(update={"original_name": bundle_original_name(bundle)})
        if skill_id is None:
            return await self._create_from_bundle(user, bundle, source, display_name)

        skill = await self.require_owned(skill_id, user)
        if expected_updated_at and skill.updated_at.isoformat() != expected_updated_at:
            raise UserSkillConflictError(
                "This skill changed since you previewed the import. Review it again."
            )
        # Fails closed: a re-import replaces everything, so it must not run
        # unless what it replaces is already a version (spec D2).
        await self._checkpoint(skill_id, user, live=skill, required=True)

        old_refs = list(skill.resources)
        new_refs = await self.bundle_files.put_imported_files(skill_id, bundle.files, old_refs)
        updated = await self.repository.update_skill(
            skill_id,
            {
                "description": bundle.description,
                "instructions": bundle.instructions,
                "allowed_tools": list(bundle.allowed_tools),
                "skill_metadata": dict(bundle.skill_metadata),
                "resources": new_refs,
                "import_source": source,
            },
            admin_user_id=user.user_id,
        )
        if updated is None:
            raise UserSkillNotFoundError(f"Skill '{skill_id}' not found")
        self.bundle_files.write_skill_md(updated)
        self._invalidate(skill_id)
        await self.bundle_files.gc_orphaned(old_refs, new_refs)
        await self._capture(skill_id, user, "reimport", live=updated)
        if not is_no_op(skill, updated):
            await record_skill_event(
                AuditAction.SKILL_REFRESHED, user, skill_id, before=skill, after=updated
            )
        self._log_import(user, skill_id, source, old_refs, new_refs, created=False)
        return updated

    async def _create_from_bundle(
        self,
        user: User,
        bundle: ParsedBundle,
        source: SkillImportSource,
        display_name: Optional[str],
    ) -> SkillDefinition:
        name = (display_name or "").strip() or bundle.display_name
        existing = await self.repository.list_skills_by_owner(user.user_id)
        if len(existing) >= MAX_SKILLS_PER_USER:
            raise UserSkillLimitError(
                f"You already have the maximum of {MAX_SKILLS_PER_USER} skills. "
                "Delete one before importing another."
            )

        skill_id = await self.plan_import_skill_id(bundle, display_name)
        refs = await self.bundle_files.put_imported_files(skill_id, bundle.files, [])
        skill = SkillDefinition(
            skill_id=skill_id,
            display_name=name,
            description=bundle.description,
            instructions=bundle.instructions,
            allowed_tools=list(bundle.allowed_tools),
            skill_metadata=dict(bundle.skill_metadata),
            resources=refs,
            status=SkillStatus.ACTIVE,
            owner_id=user.user_id,
            visibility=SkillVisibility.PRIVATE,
            created_by=user.user_id,
            updated_by=user.user_id,
            import_source=source,
        )
        try:
            created = await self.repository.create_skill(skill)
        except Exception:
            # The row never existed, so nothing names these objects.
            await self.bundle_files.gc_orphaned(refs, [])
            raise
        self.bundle_files.write_skill_md(created)
        self._invalidate(skill_id)
        await self._capture(skill_id, user, "import", live=created)
        await record_skill_event(AuditAction.SKILL_IMPORTED, user, skill_id, after=created)
        self._log_import(user, skill_id, source, [], refs, created=True)
        return created

    async def plan_import_skill_id(
        self, bundle: ParsedBundle, display_name: Optional[str] = None
    ) -> str:
        """The ``skill_id`` creating a skill from ``bundle`` would get now.

        From the frontmatter ``name`` when it can be an id
        (:func:`skill_id_from_frontmatter_name`), else from the display name as
        before; suffixed on collision either way. The preview calls this to say
        what the skill's name will be; nothing is reserved, so a skill created in
        between can move it on by a suffix.
        """
        stem = skill_id_from_frontmatter_name(bundle_original_name(bundle))
        if stem is not None:
            return await self._allocate_skill_id(stem, stem=stem)
        name = (display_name or "").strip() or bundle.display_name
        return await self._allocate_skill_id(name)

    @staticmethod
    def _log_import(
        user: User,
        skill_id: str,
        source: SkillImportSource,
        old_refs: List[SkillResourceRef],
        new_refs: List[SkillResourceRef],
        *,
        created: bool,
    ) -> None:
        old = {r.path: r.content_hash for r in old_refs}
        new = {r.path: r.content_hash for r in new_refs}
        logger.info(
            f"User {scrub_log(user.email)} imported a skill bundle into {scrub_log(skill_id)}",
            extra={
                "event": "user_skill_imported",
                "skill_id": scrub_log(skill_id),
                "owner_user_id": user.user_id,
                # NB: not "created" — reserved on LogRecord, like "filename".
                "new_skill": created,
                "source_type": source.type,
                "files_added": len(set(new) - set(old)),
                "files_changed": sum(1 for k in set(new) & set(old) if new[k] != old[k]),
                "files_removed": len(set(old) - set(new)),
            },
        )

    # =========================================================================
    # Version history (docs/specs/skill-version-history.md D1/D2)
    # =========================================================================

    async def _checkpoint(
        self,
        skill_id: str,
        user: User,
        *,
        live: Optional[SkillDefinition] = None,
        required: bool = False,
    ) -> None:
        await history.checkpoint(
            skill_id,
            actor=user.user_id,
            required=required,
            live=live,
            repository=self.repository,
            resource_store=self.bundle_files.resource_store,
        )

    async def _capture(
        self,
        skill_id: str,
        user: User,
        trigger: history.VersionTrigger,
        *,
        live: Optional[SkillDefinition] = None,
    ) -> None:
        await history.capture(
            skill_id,
            actor=user.user_id,
            trigger=trigger,
            live=live,
            repository=self.repository,
            resource_store=self.bundle_files.resource_store,
        )

    # =========================================================================
    # Internals
    # =========================================================================

    async def _allocate_skill_id(self, display_name: str, *, stem: Optional[str] = None) -> str:
        """Pick a globally-unique ``skill_id`` from a display-name stem (or ``stem``).

        Collisions are resolved by suffixing rather than rejected, so a user
        naming their skill "docx" when the catalog already has one succeeds
        (as ``docx_2``) instead of hitting a 409 that would also disclose the
        existence of a skill they cannot see. A ``stem`` already shaped like an
        id (an imported name) is used as is, and cut short only to make room
        for a suffix.
        """
        stem = stem or slugify_skill_id(display_name)
        candidate = stem
        for suffix in range(2, 100):
            if not await self.repository.skill_exists(candidate):
                return candidate
            tail = f"_{suffix}"
            candidate = f"{stem[: 50 - len(tail)].rstrip('_')}{tail}"

        # Astronomically unlikely; fall back to a random tail rather than loop.
        import uuid

        candidate = f"{stem}_{uuid.uuid4().hex[:6]}"
        if not _SKILL_ID_RE.match(candidate):
            candidate = f"{_FALLBACK_STEM}_{uuid.uuid4().hex[:8]}"
        return candidate

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
