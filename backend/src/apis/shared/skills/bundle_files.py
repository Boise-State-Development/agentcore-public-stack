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
from typing import Iterable, List, Optional, Protocol, Tuple

from apis.shared.auth.models import User
from apis.shared.blocking_io import run_blocking
from apis.shared.extraction.field_sets import validate_field_set_upload
from apis.shared.workflows.format import validate_workflow_upload
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
    resource_key,
    skill_md_key,
)
from apis.shared.skills.resource_paths import (
    FILENAME_PATTERN,
    lookup_resource,
    validate_resource_path,
)
from apis.shared.skills.resource_types import resolve_upload_content_type

logger = logging.getLogger(__name__)

VALID_RESOURCE_KINDS = ("reference", "script", "asset")

# Reference-file guardrails. Reference files are small read-only docs
# (markdown/text), not bulk assets — keep both axes modest so a skill's
# manifest stays well inside the DynamoDB item limit and the agent's
# progressive-disclosure budget (PR-6) stays bounded.
MAX_RESOURCE_BYTES = 1_048_576  # 1 MiB per file
# 64 holds anthropics/skills' Office bundles (52–60 files with their 39 OOXML
# schemas). A manifest entry is a few hundred bytes, so 64 stays far inside the
# item limit; each file adds one line to the skill's activation result.
MAX_RESOURCES_PER_SKILL = 64
# Safe, flat filenames only — no path separators, no traversal. Mirrors the
# skill_id discipline: visible, predictable object keys. A nested ``path``
# applies the same rule per segment (``resource_paths``).
_FILENAME_PATTERN = FILENAME_PATTERN


class ImportedFile(Protocol):
    """One file of an imported bundle (``bundle_import.BundleFile``'s shape)."""

    filename: str
    kind: str
    content: bytes
    content_hash: str
    # Below the kind directory; equals ``filename`` for a top-level file.
    resource_path: str


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
        path: Optional[str] = None,
    ) -> List[SkillResourceRef]:
        """Upload (or replace) one supporting file and update the manifest.

        ``path`` places the file below its kind directory (``legal/nda.md``); it
        defaults to ``filename``, and when given, ``filename`` is ignored in
        favour of its last segment. Paths are unique across kinds, as filenames
        were: re-uploading a path under another kind moves it.

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

        if path:
            validate_resource_path(path)
            filename = path.rsplit("/", 1)[-1]
        else:
            self.validate_filename(filename)
            path = filename
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
        if kind == "reference" and "/" not in path:
            # A field set is checked here, on every write path (the user and
            # admin uploads and the agent's my_skill_write_file), whatever any
            # flag says: one stored invalid fails later, mid-turn, in front of
            # someone who did not write it (field-set-authoring §4.2, D6).
            # Only the top of `references/` is read for field sets, so other
            # kinds and nested files pass.
            # FieldSetError is a ValueError, so it reaches the author as a 400
            # or a tool error carrying the parser's own message.
            validate_field_set_upload(filename, content)
            # A saved workflow (workflow-designer §9.1) is checked beside it, by
            # the same rule: whatever the flags say, at the top of `references/`.
            # WorkflowError is a ValueError too.
            validate_workflow_upload(filename, content)

        existing = list(skill.resources)
        # Adding a NEW filename must respect the per-skill cap; replacing an
        # existing filename is always allowed.
        is_new = all(r.path != path for r in existing)
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
            path=path,
        )
        new_ref = SkillResourceRef(
            filename=filename,
            content_hash=digest,
            size=len(content),
            content_type=resolved_type,
            s3_key=s3_key,
            kind=kind,
            path=path,
        )

        new_resources = [r for r in existing if r.path != path]
        new_resources.append(new_ref)
        new_resources.sort(key=manifest_sort_key)

        await self._persist_resources(skill_id, new_resources, actor)
        self._gc_orphaned(existing, new_resources)

        logger.info(
            f"Admin {scrub_log(actor.email)} uploaded reference file to skill {scrub_log(skill_id)}",
            extra={
                "event": "skill_resource_added",
                "skill_id": scrub_log(skill_id),
                # NB: not "filename" — that key is reserved on LogRecord and
                # raises KeyError when the record is actually emitted.
                "resource_filename": scrub_log(path),
                "size": len(content),
                "admin_user_id": actor.user_id,
            },
        )
        return new_resources

    async def put_imported_files(
        self,
        skill_id: str,
        files: Iterable[ImportedFile],
        existing: List[SkillResourceRef],
    ) -> List[SkillResourceRef]:
        """Store a bundle import's files and return the manifest that names them.

        The bulk form of :meth:`add_resource` for docs/specs/skill-bundle-import.md
        §6: every file is checked by the same rules (kind, filename, type
        allowlist, size, field set), a file whose bytes, kind and key already
        match ``existing`` is not written again, and the S3 puts run in a worker
        thread so a 50-file import never holds app-api's event loop. It does not
        write the manifest: the caller writes it in the same row update as the
        skill's fields, then calls :meth:`gc_orphaned`.

        Raises:
            ValueError: A file breaks a rule, or the manifest would exceed the cap.
        """
        current = {r.path: r for r in existing}
        refs: List[SkillResourceRef] = []
        for f in files:
            if f.kind not in VALID_RESOURCE_KINDS:
                raise ValueError(f"Invalid resource kind '{f.kind}'.")
            path = getattr(f, "resource_path", "") or f.filename
            validate_resource_path(path)
            if path.rsplit("/", 1)[-1] != f.filename:
                raise ValueError(f"'{path}' does not end in its filename '{f.filename}'.")
            resolved_type = resolve_upload_content_type(f.filename)
            if not f.content or len(f.content) > MAX_RESOURCE_BYTES:
                raise ValueError(
                    f"'{path}' must be between 1 and {MAX_RESOURCE_BYTES} bytes."
                )
            if f.kind == "reference" and "/" not in path:
                validate_field_set_upload(f.filename, f.content)
                validate_workflow_upload(f.filename, f.content)

            key = resource_key(skill_id, f.kind, path)
            ref = current.get(path)
            if ref is not None and (ref.content_hash, ref.kind, ref.s3_key) == (
                f.content_hash,
                f.kind,
                key,
            ):
                refs.append(ref)
                continue

            s3_key = await run_blocking(
                lambda f=f, t=resolved_type, p=path: self.resource_store.put(
                    skill_id=skill_id,
                    filename=f.filename,
                    content=f.content,
                    content_type=t,
                    kind=f.kind,
                    path=p,
                )
            )
            refs.append(
                SkillResourceRef(
                    filename=f.filename,
                    content_hash=compute_content_hash(f.content),
                    size=len(f.content),
                    content_type=resolved_type,
                    s3_key=s3_key,
                    kind=f.kind,
                    path=path,
                )
            )

        if len({r.path for r in refs}) != len(refs):
            raise ValueError("Two imported files share a path.")
        if len(refs) > MAX_RESOURCES_PER_SKILL:
            raise ValueError(
                f"A skill holds at most {MAX_RESOURCES_PER_SKILL} files; this import has {len(refs)}."
            )
        refs.sort(key=manifest_sort_key)
        return refs

    async def gc_orphaned(
        self,
        old_resources: List[SkillResourceRef],
        new_resources: List[SkillResourceRef],
    ) -> None:
        """:meth:`_gc_orphaned` in a worker thread (one S3 delete per dropped key)."""
        await run_blocking(self._gc_orphaned, old_resources, new_resources)

    async def read_resource(
        self, skill_id: str, filename: str
    ) -> Tuple[SkillResourceRef, bytes]:
        """Return one reference file's manifest entry and its bytes.

        ``filename`` is the file's path below its kind directory
        (``legal/nda.md``) or, when exactly one file has it, a bare filename.

        Raises:
            ValueError: If the skill or the named file does not exist, or a bare
                name matches more than one file.
        """
        skill = await self.repository.get_skill(skill_id)
        if skill is None:
            raise ValueError(f"Skill '{skill_id}' not found")

        ref = find_resource(skill.resources, filename, skill_id)
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
        target = find_resource(existing, filename, skill_id)

        new_resources = [r for r in existing if r.path != target.path]
        await self._persist_resources(skill_id, new_resources, actor)
        self._gc_orphaned(existing, new_resources)

        logger.info(
            f"Admin {scrub_log(actor.email)} deleted reference file from skill {scrub_log(skill_id)}",
            extra={
                "event": "skill_resource_deleted",
                "skill_id": scrub_log(skill_id),
                # NB: not "filename" — reserved on LogRecord (see add_resource).
                "resource_filename": scrub_log(target.path),
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

        The DynamoDB row is the source of truth; this projection keeps a
        conformant ``SKILL.md`` beside the skill's files (the prefix itself is
        named by ``skill_id``, not ``name``, so it is storage rather than a
        conformant bundle; an export is that). The model never reads it. When storage
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
    def validate_path(path: str) -> None:
        """Reject a path below the kind directory that breaks ``resource_paths``' rule."""
        validate_resource_path(path)

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


def manifest_sort_key(ref: SkillResourceRef) -> Tuple[str, str]:
    """Manifest order: by filename, as always, then by path for same-named files.

    A manifest of top-level files (unique filenames) sorts exactly as before, so
    the listing a skill's activation shows does not move under the prompt cache.
    """
    return (ref.filename, ref.path)


def find_resource(
    resources: List[SkillResourceRef], wanted: str, skill_id: str
) -> SkillResourceRef:
    """The entry ``wanted`` names (a path, or a bare filename only one file has).

    Raises:
        ValueError: No such file, or a bare name two files share (both named).
    """
    found = lookup_resource(resources, wanted or "")
    if found.ref is not None:
        return found.ref
    if found.ambiguous:
        paths = ", ".join(r.path for r in found.ambiguous)
        raise ValueError(
            f"More than one file in skill '{skill_id}' is named '{wanted}' ({paths}); "
            "name it by its path"
        )
    raise ValueError(f"Reference file '{wanted}' not found on skill '{skill_id}'")


_bundle_files_instance: Optional[SkillBundleFiles] = None


def get_skill_bundle_files() -> SkillBundleFiles:
    """Get or create the global SkillBundleFiles instance."""
    global _bundle_files_instance
    if _bundle_files_instance is None:
        _bundle_files_instance = SkillBundleFiles()
    return _bundle_files_instance
