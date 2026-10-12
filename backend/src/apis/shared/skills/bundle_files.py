"""Skill bundle files: the one write path for a skill's S3 bundle.

Both authorship tiers (the admin catalog and user-authored "My Skills") store
their supporting files in the same agentskills.io layout
(``skills/{id}/{references|scripts|assets}/{filename}`` plus a ``SKILL.md``
projection), with the manifest on the catalog row. This class owns that layout:
caps, the filename and type allowlist, the manifest write, the projection and
orphan GC.

It lives in ``apis.shared`` (skill-creator spec D5) so that a runtime tool can
write a user's skill through exactly the code app-api's upload routes use. It
carries **no access decision**: callers check the tier first
(``SkillCatalogService.require_catalog_skill`` for admins,
``UserSkillService.require_owned`` for authors), then call in here. It also has
no RBAC dependency, so building it does not build ``AppRoleAdminService``.
"""

import logging
import re
from typing import List, Optional, Tuple

from apis.shared.auth.models import User
from apis.shared.security.log_sanitize import scrub_log
from apis.shared.skills.bundle import generate_skill_md
from apis.shared.skills.models import SkillDefinition, SkillResourceRef
from apis.shared.skills.repository import (
    SkillCatalogRepository,
    get_skill_catalog_repository,
)
from apis.shared.skills.resource_store import (
    SkillResourceStore,
    SkillResourceStoreError,
    compute_content_hash,
    get_skill_resource_store,
    skill_md_key,
)
from apis.shared.skills.resource_types import resolve_upload_content_type

logger = logging.getLogger(__name__)

VALID_RESOURCE_KINDS = ("reference", "script", "asset")

# Reference-file guardrails. Reference files are small read-only docs
# (markdown/text), not bulk assets — keep both axes modest so a skill's
# manifest stays well inside the DynamoDB item limit and the agent's
# progressive-disclosure budget (PR-6) stays bounded.
MAX_RESOURCE_BYTES = 1_048_576  # 1 MiB per file
MAX_RESOURCES_PER_SKILL = 50
# Safe, flat filenames only — no path separators, no traversal. Mirrors the
# skill_id discipline: visible, predictable object keys.
_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class SkillBundleFiles:
    """Read and write one skill's bundle files and its manifest."""

    def __init__(
        self,
        repository: Optional[SkillCatalogRepository] = None,
        resource_store: Optional[SkillResourceStore] = None,
    ):
        self.repository = repository or get_skill_catalog_repository()
        self.resource_store = resource_store or get_skill_resource_store()

    async def add_resource(
        self,
        skill_id: str,
        filename: str,
        content: bytes,
        content_type: str,
        actor: User,
        kind: str = "reference",
    ) -> List[SkillResourceRef]:
        """Upload (or replace) one supporting file and update the manifest.

        Bytes are stored in the standard agentskills.io bundle layout
        (``skills/{id}/{references|scripts|assets}/{filename}``); the manifest on
        the catalog row is updated atomically (single row write). Re-uploading
        the same filename replaces its manifest entry (and overwrites its object
        in place); a file whose ``(kind, filename)`` key changed is
        garbage-collected. ``script`` files are accept-and-inert (stored,
        listed, never executed — D5).

        SECURITY — the caller-supplied ``content_type`` is accepted for
        signature compatibility and then **ignored**. The stored type is derived
        from the filename extension against
        ``apis.shared.skills.resource_types``' allowlist, because both tiers'
        upload routes are reachable by an unprivileged author while the bytes
        are downloaded by other users from the SPA's own origin. Letting a
        caller label their upload ``text/html`` made an uploaded file a
        same-origin script-execution primitive against whoever opened it.

        Returns the skill's updated manifest.

        Raises:
            ValueError: If the skill is missing, the kind or filename is invalid,
                the file type is not allowed
                (:class:`~apis.shared.skills.resource_types.SkillResourceTypeError`),
                the file is too large, or the per-skill file cap is exceeded.
        """
        if kind not in VALID_RESOURCE_KINDS:
            raise ValueError(
                f"Invalid resource kind '{kind}'. Expected one of "
                f"{', '.join(VALID_RESOURCE_KINDS)}."
            )

        skill = await self.repository.get_skill(skill_id)
        if skill is None:
            raise ValueError(f"Skill '{skill_id}' not found")

        self.validate_filename(filename)
        # Type allowlist before any byte handling: an unacceptable filename is
        # rejected without ever reaching S3 or the manifest.
        resolved_type = resolve_upload_content_type(filename)
        if len(content) > MAX_RESOURCE_BYTES:
            raise ValueError(
                f"Reference file '{filename}' is {len(content)} bytes; "
                f"the limit is {MAX_RESOURCE_BYTES} bytes."
            )
        if not content:
            raise ValueError(f"Reference file '{filename}' is empty.")

        existing = list(skill.resources)
        # Adding a NEW filename must respect the per-skill cap; replacing an
        # existing filename is always allowed.
        is_new = all(r.filename != filename for r in existing)
        if is_new and len(existing) >= MAX_RESOURCES_PER_SKILL:
            raise ValueError(
                f"Skill '{skill_id}' already has the maximum of "
                f"{MAX_RESOURCES_PER_SKILL} reference files."
            )

        digest = compute_content_hash(content)
        s3_key = self.resource_store.put(
            skill_id=skill_id,
            filename=filename,
            content=content,
            content_type=resolved_type,
            kind=kind,
        )
        new_ref = SkillResourceRef(
            filename=filename,
            content_hash=digest,
            size=len(content),
            content_type=resolved_type,
            s3_key=s3_key,
            kind=kind,
        )

        new_resources = [r for r in existing if r.filename != filename]
        new_resources.append(new_ref)
        new_resources.sort(key=lambda r: r.filename)

        await self._persist_resources(skill_id, new_resources, actor)
        self._gc_orphaned(existing, new_resources)

        logger.info(
            f"Admin {scrub_log(actor.email)} uploaded reference file to skill {scrub_log(skill_id)}",
            extra={
                "event": "skill_resource_added",
                "skill_id": scrub_log(skill_id),
                # NB: not "filename" — that key is reserved on LogRecord and
                # raises KeyError when the record is actually emitted.
                "resource_filename": scrub_log(filename),
                "size": len(content),
                "admin_user_id": actor.user_id,
            },
        )
        return new_resources

    async def read_resource(
        self, skill_id: str, filename: str
    ) -> Tuple[SkillResourceRef, bytes]:
        """Return one reference file's manifest entry and its bytes.

        Raises:
            ValueError: If the skill or the named file does not exist.
        """
        skill = await self.repository.get_skill(skill_id)
        if skill is None:
            raise ValueError(f"Skill '{skill_id}' not found")

        ref = next((r for r in skill.resources if r.filename == filename), None)
        if ref is None:
            raise ValueError(
                f"Reference file '{filename}' not found on skill '{skill_id}'"
            )

        content = self.resource_store.get(ref.s3_key)
        return ref, content

    async def delete_resource(
        self, skill_id: str, filename: str, actor: User
    ) -> List[SkillResourceRef]:
        """Remove one reference file from the manifest (and GC its object).

        Returns the skill's updated manifest.

        Raises:
            ValueError: If the skill or the named file does not exist.
        """
        skill = await self.repository.get_skill(skill_id)
        if skill is None:
            raise ValueError(f"Skill '{skill_id}' not found")

        existing = list(skill.resources)
        if all(r.filename != filename for r in existing):
            raise ValueError(
                f"Reference file '{filename}' not found on skill '{skill_id}'"
            )

        new_resources = [r for r in existing if r.filename != filename]
        await self._persist_resources(skill_id, new_resources, actor)
        self._gc_orphaned(existing, new_resources)

        logger.info(
            f"Admin {scrub_log(actor.email)} deleted reference file from skill {scrub_log(skill_id)}",
            extra={
                "event": "skill_resource_deleted",
                "skill_id": scrub_log(skill_id),
                # NB: not "filename" — reserved on LogRecord (see add_resource).
                "resource_filename": scrub_log(filename),
                "admin_user_id": actor.user_id,
            },
        )
        return new_resources

    async def delete_versions(self, skill_id: str) -> None:
        """Drop a hard-deleted skill's pinned versions and their frozen files.

        Versions must not outlive the skill (shared-projects 3.1): skill ids can be
        reused, and a project still pinned to ``id@2`` would otherwise run the old
        skill's instructions under the new one's live status. Frozen files are
        content-addressed and private to the skill, so all of them go. Best-effort on
        S3: an orphaned object is only storage.
        """
        deleted = await self.repository.delete_skill_versions(skill_id)
        for key in sorted({ref.s3_key for version in deleted for ref in version.resources}):
            self.resource_store.delete(key)

    def write_skill_md(self, skill: SkillDefinition) -> None:
        """Write the skill's SKILL.md projection to S3 (best-effort).

        The DynamoDB row is the source of truth; this projection exists only so
        the S3 prefix is a valid, portable agentskills.io bundle. When storage
        is unconfigured (local dev) or the write fails, log and continue — never
        fail the catalog write on the projection.
        """
        if not self.resource_store.enabled:
            return
        try:
            content = generate_skill_md(
                skill_id=skill.skill_id,
                description=skill.description,
                instructions=skill.instructions,
                allowed_tools=skill.allowed_tools,
                skill_metadata=skill.skill_metadata,
            )
            self.resource_store.put_skill_md(skill_id=skill.skill_id, content=content)
        except SkillResourceStoreError:
            logger.warning(
                "skill-resources: SKILL.md projection failed for skill=%s "
                "(row is source of truth; continuing)",
                skill.skill_id,
                exc_info=True,
            )

    def delete_skill_md(self, skill_id: str) -> None:
        """Best-effort removal of the SKILL.md projection for a deleted skill."""
        self.resource_store.delete(skill_md_key(skill_id))

    @staticmethod
    def validate_filename(filename: str) -> None:
        """Reject path traversal / unsafe filenames up front."""
        if not _FILENAME_PATTERN.match(filename or ""):
            raise ValueError(
                f"Invalid reference filename '{filename}'. Use letters, "
                "digits, '.', '_' and '-' only (no path separators), "
                "1-128 characters."
            )

    async def _persist_resources(
        self,
        skill_id: str,
        resources: List[SkillResourceRef],
        actor: User,
    ) -> None:
        """Write the manifest to the catalog row (single atomic item write).

        The SKILL.md projection is NOT rewritten here: its frontmatter does not
        list resources, so a manifest change never alters it.
        """
        await self.repository.update_skill(
            skill_id, {"resources": resources}, admin_user_id=actor.user_id
        )

    def _gc_orphaned(
        self,
        old_resources: List[SkillResourceRef],
        new_resources: List[SkillResourceRef],
    ) -> None:
        """Delete S3 objects no longer referenced by the new manifest.

        Keys are now path-based (``.../{kind}/{filename}``), so a removed file —
        or one whose kind changed — orphans its old key. Best-effort: a failed
        cleanup never fails the write; an orphaned object is only wasted storage.
        """
        old_keys = {r.s3_key for r in old_resources}
        new_keys = {r.s3_key for r in new_resources}
        for key in old_keys - new_keys:
            self.resource_store.delete(key)


_bundle_files_instance: Optional[SkillBundleFiles] = None


def get_skill_bundle_files() -> SkillBundleFiles:
    """Get or create the global SkillBundleFiles instance."""
    global _bundle_files_instance
    if _bundle_files_instance is None:
        _bundle_files_instance = SkillBundleFiles()
    return _bundle_files_instance
