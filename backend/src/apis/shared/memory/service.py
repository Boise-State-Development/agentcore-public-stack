"""Service layer for Memory Spaces (PR-1, data layer).

Owns space lifecycle, permission resolution, sharing, and entry/index I/O —
composing the DynamoDB repository (``repository.py``) with the S3 byte store
(``store.py``). This is the data-layer API that the runtime read/write path
(PR-2/PR-4) and the app-api user surface (PR-5) call; PR-1 adds no routes,
tools, or system-prompt wiring.

**Access control is identity-based and enforced here**, at the one chokepoint
``resolve_permission`` — mirroring ``resolve_assistant_permission``. The owner
is stored on the space; shared grants are ``viewer``/``editor`` member rows.
Every read requires ``viewer+``; every write requires ``editor+``; sharing and
deletion require ``owner``. There is no content inspection — governance is the
grant, consistent with how the platform treats every other shared entity.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

from .format import (
    Frontmatter,
    MemoryFormatError,
    frontmatter_from_parsed,
    parse_file,
    render_file,
    validate_slug,
)
from .models import (
    EntryType,
    FileFormat,
    FileVersion,
    FileVersionReason,
    MemoryEntryRef,
    MemoryIndex,
    MemoryScope,
    MemorySpace,
    Role,
    ShareRole,
    SpaceMember,
)
from .repository import ManifestTooLargeError, MemorySpaceRepository, OptimisticLockError
from .store import (
    MemorySpaceStore,
    MemorySpaceStoreError,
    compute_content_hash,
    content_key,
    get_memory_space_store,
)
from .templates import DEFAULT_TEMPLATE_ID, get_template, is_valid_template
from .tokens import TokenCount, count_file_tokens
from .validation import (
    CanonicalSave,
    CurrentFile,
    check_name_collisions,
    freeform_link_warnings,
    validate_canonical_save,
    validate_index_links,
)

logger = logging.getLogger(__name__)

_ROLE_RANK: Dict[str, int] = {"viewer": 1, "editor": 2, "owner": 3}

# Bounded read-modify-retry attempts when a shared space's manifest is being
# edited concurrently. Entry writes touch a single slug, so re-reading the
# fresh manifest and re-applying the change is safe; only a sustained race
# exhausts this and surfaces as a conflict.
_MAX_MANIFEST_RETRIES = 5

# Default soft cap on the number of entries (≈ index lines). Consolidation
# reports when a space is over it — it never auto-evicts (that's a judgment
# call for the future LLM pass). ≈ 200 entries ≈ 4k always-loaded tokens/turn.
_DEFAULT_INDEX_CAP = 200

# Wikilinks in MEMORY.md: [[slug]] pointers into the entry set.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")

_T = TypeVar("_T")

# Per-file token thresholds (Shared Projects §4.6). A canonical file over the
# hard cap is rejected; crossing the soft threshold is reported. Freeform
# entries only get warnings, so spaces written before 2.3 keep working.
_DEFAULT_FILE_HARD_CAP_TOKENS = 8_000
_DEFAULT_FILE_SOFT_THRESHOLD_PCT = 75

_FILE_FORMATS = ("freeform", "canonical")


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw:
        try:
            return max(minimum, int(raw))
        except ValueError:
            logger.warning("invalid %s=%r; using default", name, raw)
    return default


def file_hard_cap_tokens() -> int:
    return _env_int("MEMORY_FILE_HARD_CAP_TOKENS", _DEFAULT_FILE_HARD_CAP_TOKENS, minimum=1)


def file_soft_threshold_tokens() -> int:
    pct = min(100, _env_int("MEMORY_FILE_SOFT_THRESHOLD_PCT", _DEFAULT_FILE_SOFT_THRESHOLD_PCT, minimum=1))
    return file_hard_cap_tokens() * pct // 100


def _next_version(ref: Optional[MemoryEntryRef]) -> int:
    """The version number a save of ``ref``'s slug commits.

    An entry written before history existed has version 0 and no rows; its
    old content becomes version 1 (``baseline``) and the save version 2.
    """
    if ref is None:
        return 1
    return ref.version + 1 if ref.version else 2


def _index_cap() -> int:
    """Soft entry cap, overridable via ``MEMORY_SPACE_INDEX_CAP``."""
    raw = os.environ.get("MEMORY_SPACE_INDEX_CAP")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            logger.warning("invalid MEMORY_SPACE_INDEX_CAP=%r; using default", raw)
    return _DEFAULT_INDEX_CAP


class MemorySpaceError(RuntimeError):
    """Base class for memory-space service errors (translated by the API layer)."""


class MemorySpaceNotFoundError(MemorySpaceError):
    """The space does not exist (or the caller may not even know it does)."""


class MemoryEntryNotFoundError(MemorySpaceNotFoundError):
    """The space is there and readable, but the entry is not.

    A subclass, so every caller that catches :class:`MemorySpaceNotFoundError`
    behaves as before; the project harness's tools tell the two apart.
    """


class MemorySpacePermissionError(MemorySpaceError):
    """The caller lacks the required role on the space."""


class MemorySpaceConcurrencyError(MemorySpaceError):
    """A shared space's manifest kept changing under a bounded retry loop.

    Surfaced to the API layer as ``409 Conflict`` — the write is safe to retry
    from a fresh read.
    """


class MemoryValidationError(MemorySpaceError):
    """A save failed validation (§4.3); nothing was written.

    ``code`` is the stable reason (``prose_in_body``, ``over_hard_cap``, …).
    The message is meant for the person or agent who made the save.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code

    @classmethod
    def from_format_error(cls, exc: MemoryFormatError) -> "MemoryValidationError":
        return cls(str(exc), code=exc.code)


@dataclass
class SaveResult:
    """What a save wrote, plus what the caller should hear about it."""

    ref: MemoryEntryRef
    warnings: List[str] = field(default_factory=list)
    minted_anchors: List[str] = field(default_factory=list)
    removed_anchors: List[str] = field(default_factory=list)
    archived_links: List[str] = field(default_factory=list)
    over_soft_threshold: bool = False


@dataclass
class MemorySpaceExport:
    """The full readable corpus of a space, gathered for a `.zip` download (§9).

    Loss-free snapshot: the space metadata, the ``MEMORY.md`` index text, and
    every entry paired with its raw bytes (frontmatter intact). ``members`` is
    populated only for editor+ callers — a viewer gets the corpus without the
    grant list, mirroring the ``list_members`` gate.
    """

    space: MemorySpace
    role: Role
    index_text: str
    files: List[Tuple[MemoryEntryRef, bytes]] = field(default_factory=list)
    members: List[SpaceMember] = field(default_factory=list)


@dataclass
class ConsolidationReport:
    """Result of a deterministic consolidation (health) pass over a space (A6).

    The pass auto-fixes only storage hygiene — orphaned content-addressed
    objects with no manifest/index reference are deleted (``orphans_deleted``).
    Everything that needs a judgment call is *reported*, not mutated:
    ``duplicate_groups`` (entries sharing a content hash — which slug survives
    is semantic), ``dead_links`` (``[[slug]]`` pointers in MEMORY.md with no
    entry), and ``over_cap`` (entry count past the soft index cap — which entry
    to drop is semantic). The LLM consolidation pass (Workstream B) extends this
    seam to act on those reports.
    """

    space_id: str
    entry_count: int
    index_cap: int
    over_cap: bool
    orphans_deleted: int = 0
    duplicate_groups: List[List[str]] = field(default_factory=list)
    dead_links: List[str] = field(default_factory=list)
    stripped_dead_links: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_space_id() -> str:
    return f"spc_{uuid.uuid4().hex}"


def _get_nested(data: Dict[str, Any], dotted: str) -> Any:
    """Resolve a dotted key (``commitments.due``) against a nested dict."""
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


class MemorySpaceService:
    """Lifecycle + permission + I/O for Memory Spaces."""

    def __init__(
        self,
        repository: Optional[MemorySpaceRepository] = None,
        store: Optional[MemorySpaceStore] = None,
        token_counter: Optional[Callable[[str], TokenCount]] = None,
    ) -> None:
        self.repository = repository or MemorySpaceRepository()
        self.store = store or get_memory_space_store()
        self._count_tokens = token_counter or count_file_tokens

    # ---- permission ----------------------------------------------------

    def resolve_permission(
        self, space_id: str, user_id: str, user_email: Optional[str] = None
    ) -> Tuple[Optional[MemorySpace], Optional[Role]]:
        """Resolve the caller's role on a space.

        Returns ``(space, role)`` where role is ``owner``/``editor``/``viewer``,
        or ``(space, None)`` if the caller has no grant, or ``(None, None)`` if
        the space does not exist. Mirrors ``resolve_assistant_permission``.

        A project's spaces take their role from the project instead
        (:meth:`_resolve_project_role`), and resolve as missing for anyone
        outside it.
        """
        space = self.repository.get_space(space_id)
        if space is None:
            return None, None
        if space.is_project_space:
            role = self._resolve_project_role(space, user_id, user_email)
            return (space, role) if role is not None else (None, None)
        if space.owner_id == user_id:
            return space, "owner"
        if user_email:
            member = self.repository.get_member(space_id, user_email)
            if member is not None:
                return space, member.permission
        return space, None

    @staticmethod
    def _resolve_project_role(
        space: MemorySpace, user_id: str, user_email: Optional[str]
    ) -> Optional[Role]:
        """A project space's role, from the project's membership (§3.3).

        No ``MEMBER#`` rows are written for these spaces, so membership has one
        source of truth. Nobody resolves ``owner``: deleting and sharing belong
        to the project, not to the space. The shared space gives editors (the
        project owner included) ``editor`` and viewers ``viewer``; a
        ``personal_in_project`` space gives its member ``editor`` while they
        belong to the project, and nobody else anything. An archived project's
        spaces are read-only, and while Shared Projects is off they resolve
        for no one.
        """
        from apis.shared.feature_flags import projects_enabled
        from apis.shared.projects.access import resolve_project_role

        if not projects_enabled() or not space.project_id:
            return None
        if space.scope == "personal_in_project" and space.user_id != user_id:
            return None
        project, project_role = resolve_project_role(space.project_id, user_id, user_email)
        if project is None or project_role is None:
            return None
        if project.status == "archived":
            return "viewer"
        if space.scope == "personal_in_project":
            return "editor"
        return "viewer" if project_role == "viewer" else "editor"

    def _require_own_space(
        self,
        space_id: str,
        user_id: str,
        user_email: Optional[str],
        min_role: Role,
    ) -> Tuple[MemorySpace, Role]:
        """:meth:`_require` for actions a project space never allows directly.

        Sharing, leaving and deleting a project's space go through the project
        (its members and its purge), so a member who can see the space is told
        where to go instead of getting a bare 403.
        """
        space, _ = self.resolve_permission(space_id, user_id, user_email)
        if space is not None and space.is_project_space:
            raise MemorySpaceError(
                "This memory belongs to a project. Its access follows the project's "
                "members, and it is deleted with the project."
            )
        return self._require(space_id, user_id, user_email, min_role)

    def _require(
        self,
        space_id: str,
        user_id: str,
        user_email: Optional[str],
        min_role: Role,
    ) -> Tuple[MemorySpace, Role]:
        space, role = self.resolve_permission(space_id, user_id, user_email)
        if space is None:
            raise MemorySpaceNotFoundError(f"Memory space '{space_id}' not found")
        if role is None or _ROLE_RANK[role] < _ROLE_RANK[min_role]:
            raise MemorySpacePermissionError(
                f"'{min_role}' access required on memory space '{space_id}'"
            )
        return space, role

    # ---- lifecycle -----------------------------------------------------

    def create_space(
        self,
        owner_id: str,
        owner_email: str,
        name: str,
        template: str = DEFAULT_TEMPLATE_ID,
        file_format: FileFormat = "freeform",
    ) -> MemorySpace:
        """Create a space seeded from a template; returns the persisted space.

        ``file_format="canonical"`` makes every entry an item list checked on
        save (Shared Projects §4.2). It is fixed for the life of the space.
        """
        if not owner_id:
            raise MemorySpaceError("owner_id is required to create a space")
        if not name or not name.strip():
            raise MemorySpaceError("a memory space name is required")
        if not is_valid_template(template):
            raise MemorySpaceError(f"unknown template '{template}'")
        if file_format not in _FILE_FORMATS:
            raise MemorySpaceError(f"unknown file format '{file_format}'")
        return self._create(owner_id, owner_email, name, template, file_format)

    def create_project_space(
        self,
        *,
        project_id: str,
        scope: MemoryScope,
        owner_id: str,
        owner_email: str,
        name: str,
        user_id: Optional[str] = None,
    ) -> MemorySpace:
        """Create one of a project's spaces. No permission check: the project decides.

        Called by ``apis.shared.projects`` only, never from a user-facing route.
        Project spaces are always ``canonical`` (Shared Projects §4.2). The
        owner fields record who created the space; nothing reads them for
        access. ``user_id`` is required for ``personal_in_project``.
        """
        if scope not in ("shared", "personal_in_project"):
            raise MemorySpaceError(f"'{scope}' is not a project scope")
        if not project_id:
            raise MemorySpaceError("project_id is required for a project space")
        if scope == "personal_in_project" and not user_id:
            raise MemorySpaceError("user_id is required for a personal project space")
        if not name or not name.strip():
            raise MemorySpaceError("a memory space name is required")
        return self._create(
            owner_id,
            owner_email,
            name,
            DEFAULT_TEMPLATE_ID,
            "canonical",
            scope=scope,
            project_id=project_id,
            user_id=user_id if scope == "personal_in_project" else None,
        )

    def _create(
        self,
        owner_id: str,
        owner_email: str,
        name: str,
        template: str,
        file_format: FileFormat,
        *,
        scope: MemoryScope = "personal",
        project_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> MemorySpace:
        tmpl = get_template(template)
        space_id = _new_space_id()
        now = _now_iso()

        # Seed the human-readable MEMORY.md index in S3.
        index_bytes = tmpl.starter_index.encode("utf-8")
        index_key = self.store.put(
            space_id=space_id, content=index_bytes, content_type="text/markdown"
        )

        space = MemorySpace(
            space_id=space_id,
            name=name.strip(),
            template=template,
            owner_id=owner_id,
            owner_email=(owner_email or "").strip().lower(),
            created_at=now,
            updated_at=now,
            index_s3_key=index_key,
            index_content_hash=compute_content_hash(index_bytes),
            file_format=file_format,
            scope=scope,
            project_id=project_id,
            user_id=user_id,
        )
        self.repository.put_space(space)
        self.repository.put_index(MemoryIndex(space_id=space_id, entries=[], version=0))
        logger.info(
            "memory-spaces: created space=%s owner=%s template=%s scope=%s project=%s",
            space_id,
            owner_id,
            template,
            scope,
            project_id,
        )
        return space

    def get_space(
        self, space_id: str, user_id: str, user_email: Optional[str] = None
    ) -> MemorySpace:
        space, _ = self._require(space_id, user_id, user_email, "viewer")
        return space

    def list_spaces_for_user(
        self, user_id: str, user_email: Optional[str] = None
    ) -> List[Tuple[MemorySpace, Role]]:
        """List ``(space, role)`` for spaces the user owns plus shared-in (deduped).

        Owned spaces resolve to ``owner``; shared-in carry the member's actual
        ``viewer``/``editor`` grant, so the SPA can render accurate affordances
        without a follow-up call per space.
        """
        result: List[Tuple[MemorySpace, Role]] = []
        seen: set[str] = set()
        for s in self.repository.list_owned(user_id):
            result.append((s, "owner"))
            seen.add(s.space_id)
        if user_email:
            for space_id in self.repository.list_member_space_ids(user_email):
                if space_id in seen:
                    continue
                shared = self.repository.get_space(space_id)
                if shared is None:
                    continue
                member = self.repository.get_member(space_id, user_email)
                result.append((shared, member.permission if member else "viewer"))
                seen.add(space_id)
        return sorted(result, key=lambda t: t[0].created_at)

    def export_space(
        self, space_id: str, user_id: str, user_email: Optional[str] = None
    ) -> MemorySpaceExport:
        """Gather the full readable corpus of a space for download (viewer+).

        Reads the manifest once and pulls every entry's bytes from the
        content-addressed store — the loss-free "own your data" export (§9).
        The app-api layer turns this into a streamed ``.zip``. Members are
        included only for editor+ callers (mirrors :meth:`list_members`); a
        viewer exports the content they can read without the grant list.
        """
        space, role = self._require(space_id, user_id, user_email, "viewer")
        index_text = ""
        if space.index_s3_key:
            index_text = self.store.get(space.index_s3_key).decode("utf-8")
        index = self.repository.get_index(space_id)
        files = [(ref, self.store.get(ref.s3_key)) for ref in index.entries]
        members = (
            self.repository.list_members(space_id)
            if _ROLE_RANK[role] >= _ROLE_RANK["editor"]
            else []
        )
        return MemorySpaceExport(
            space=space,
            role=role,
            index_text=index_text,
            files=files,
            members=members,
        )

    def delete_space(
        self, space_id: str, user_id: str, user_email: Optional[str] = None
    ) -> None:
        """Delete a space (owner only): all rows and every stored object.

        Objects are gathered from the manifest, the version history and a
        listing of the space's prefix, so neither old versions nor orphans
        from interrupted writes survive the space.
        """
        self._require_own_space(space_id, user_id, user_email, "owner")
        self._purge(space_id)
        logger.info("memory-spaces: deleted space=%s by user=%s", space_id, user_id)

    def purge_project_space(self, space_id: str) -> None:
        """Delete a project's space with no permission check (the project's purge).

        Tolerates a space that is already gone, so a retried project purge
        passes through. Refuses a personal space: only a project space may be
        deleted on the project's say-so.
        """
        space = self.repository.get_space(space_id)
        if space is None:
            return
        if not space.is_project_space:
            raise MemorySpaceError(f"memory space '{space_id}' does not belong to a project")
        self._purge(space_id)
        logger.info("memory-spaces: purged project space=%s project=%s", space_id, space.project_id)

    def rename_project_space(self, space_id: str, name: str) -> None:
        """Give a project's shared space the project's new name (no permission check)."""
        space = self.repository.get_space(space_id)
        if space is None or not space.is_project_space or not name.strip():
            return
        if space.name != name.strip():
            space.name = name.strip()
            space.updated_at = _now_iso()
            self.repository.put_space(space)

    def _purge(self, space_id: str) -> None:
        keys = self._referenced_keys(space_id)
        try:
            keys.update(self.store.list_keys(space_id))
        except MemorySpaceStoreError:
            logger.warning("memory-spaces: could not list objects of space=%s; deleting referenced ones", space_id)
        for key in keys:
            self.store.delete(key)
        self.repository.delete_space(space_id)

    def leave_space(
        self, space_id: str, user_id: str, user_email: Optional[str] = None
    ) -> None:
        """Drop the caller's own grant on a space shared with them.

        A member removes *their own* access — no owner action required (the
        "forget-me on a shared-in space = leave" case from the governance
        section). The owner cannot leave; they delete the space instead.
        """
        space, role = self.resolve_permission(space_id, user_id, user_email)
        if space is None:
            raise MemorySpaceNotFoundError(f"Memory space '{space_id}' not found")
        if space.is_project_space:
            self._require_own_space(space_id, user_id, user_email, "viewer")
        if role == "owner":
            raise MemorySpaceError(
                "the owner cannot leave a space; delete it instead"
            )
        if role is None or not user_email:
            raise MemorySpacePermissionError(
                f"you are not a member of memory space '{space_id}'"
            )
        self.repository.delete_member(space_id, user_email)
        logger.info("memory-spaces: user=%s left space=%s", user_id, space_id)

    # ---- consolidation (A6) --------------------------------------------

    def consolidate(
        self,
        space_id: str,
        user_id: str,
        user_email: Optional[str] = None,
        *,
        apply_gc: bool = True,
        strip_dead_links: bool = False,
    ) -> ConsolidationReport:
        """Deterministic consolidation (health) pass over a space (editor+).

        Auto-fixes storage hygiene — orphaned content-addressed objects (no
        manifest/index reference) are deleted when ``apply_gc``. Everything that
        needs judgment is reported, not mutated: duplicate content across slugs,
        dead ``[[slug]]`` wikilinks in MEMORY.md, and over-cap entry counts. It
        never merges or evicts entries — that's the LLM pass (Workstream B) that
        extends this seam. ``strip_dead_links`` opts into one safe edit: unlink
        dead ``[[slug]]`` pointers (they point nowhere), preserving the prose.
        """
        space, _ = self._require(space_id, user_id, user_email, "editor")
        index = self.repository.get_index(space_id)
        entries = index.entries
        slugs = {e.slug for e in entries}

        cap = _index_cap()

        # Duplicate detection: more than one slug sharing a content hash.
        by_hash: Dict[str, List[str]] = {}
        for e in entries:
            by_hash.setdefault(e.content_hash, []).append(e.slug)
        duplicate_groups = sorted(
            (sorted(s) for s in by_hash.values() if len(s) > 1),
            key=lambda g: g[0],
        )

        # Dead-link detection over the MEMORY.md text.
        index_text = ""
        if space.index_s3_key:
            index_text = self.store.get(space.index_s3_key).decode("utf-8")
        referenced = {m.strip() for m in _WIKILINK_RE.findall(index_text)}
        dead_links = sorted(ref for ref in referenced if ref and ref not in slugs)

        stripped = False
        if strip_dead_links and dead_links and space.index_s3_key:
            new_text = index_text
            for ref in dead_links:
                new_text = new_text.replace(f"[[{ref}]]", ref)
            if new_text != index_text:
                self.update_index(space_id, user_id, user_email, new_text)
                space = self.repository.get_space(space_id) or space
                stripped = True

        # Orphaned-object GC: keys under the space prefix that no entry or the
        # index pointer references (leaks from crashed/raced writes). Safe —
        # unreferenced content is invisible to every read path.
        orphans_deleted = 0
        if apply_gc:
            # Old versions are referenced by their FILEVER rows, not orphans.
            referenced_keys = self._referenced_keys(space_id, index=index, space=space)
            for key in self.store.list_keys(space_id):
                if key not in referenced_keys:
                    self.store.delete(key)
                    orphans_deleted += 1

        logger.info(
            "memory-spaces: consolidated space=%s entries=%d orphans=%d "
            "dups=%d dead_links=%d stripped=%s",
            space_id,
            len(entries),
            orphans_deleted,
            len(duplicate_groups),
            len(dead_links),
            stripped,
        )
        return ConsolidationReport(
            space_id=space_id,
            entry_count=len(entries),
            index_cap=cap,
            over_cap=len(entries) > cap,
            orphans_deleted=orphans_deleted,
            duplicate_groups=duplicate_groups,
            dead_links=dead_links,
            stripped_dead_links=stripped,
        )

    # ---- sharing -------------------------------------------------------

    def share(
        self,
        space_id: str,
        actor_id: str,
        actor_email: Optional[str],
        grantee_email: str,
        permission: ShareRole = "viewer",
    ) -> SpaceMember:
        """Grant ``grantee_email`` a role on the space (owner only)."""
        self._require_own_space(space_id, actor_id, actor_email, "owner")
        if permission not in ("viewer", "editor"):
            raise MemorySpaceError(f"invalid share permission '{permission}'")
        member = SpaceMember(
            email=grantee_email.strip().lower(),
            permission=permission,
            created_at=_now_iso(),
        )
        self.repository.put_member(space_id, member)
        self._touch(space_id)
        return member

    def update_share(
        self,
        space_id: str,
        actor_id: str,
        actor_email: Optional[str],
        grantee_email: str,
        permission: ShareRole,
    ) -> SpaceMember:
        """Change an existing grant's role (owner only), preserving its origin.

        Distinct from :meth:`share` (upsert-create) so a PATCH gets proper
        not-found semantics and keeps the original ``created_at``.
        """
        self._require_own_space(space_id, actor_id, actor_email, "owner")
        if permission not in ("viewer", "editor"):
            raise MemorySpaceError(f"invalid share permission '{permission}'")
        existing = self.repository.get_member(space_id, grantee_email)
        if existing is None:
            raise MemorySpaceNotFoundError(
                f"'{grantee_email}' is not a member of memory space '{space_id}'"
            )
        member = SpaceMember(
            email=grantee_email.strip().lower(),
            permission=permission,
            created_at=existing.created_at,
        )
        self.repository.put_member(space_id, member)
        self._touch(space_id)
        return member

    def revoke(
        self,
        space_id: str,
        actor_id: str,
        actor_email: Optional[str],
        grantee_email: str,
    ) -> None:
        """Remove a grant (owner only)."""
        self._require_own_space(space_id, actor_id, actor_email, "owner")
        self.repository.delete_member(space_id, grantee_email)
        self._touch(space_id)

    def list_members(
        self, space_id: str, user_id: str, user_email: Optional[str] = None
    ) -> List[SpaceMember]:
        """List a space's shared grants (owner or editor)."""
        self._require(space_id, user_id, user_email, "editor")
        return self.repository.list_members(space_id)

    # ---- index (MEMORY.md) ---------------------------------------------

    def read_index(
        self, space_id: str, user_id: str, user_email: Optional[str] = None
    ) -> str:
        space, _ = self._require(space_id, user_id, user_email, "viewer")
        if not space.index_s3_key:
            return ""
        return self.store.get(space.index_s3_key).decode("utf-8")

    def read_project_space_index(
        self, space_id: str, *, project_id: str, scope: MemoryScope, user_id: str
    ) -> str:
        """A project space's ``MEMORY.md`` for a caller whose project role is already settled.

        The project harness's turn path, which has resolved the caller as a
        member of an active project before it gets here. It skips
        :meth:`resolve_permission` (a project META and a ``MEMBER#`` read) and
        checks instead that the space is the one the project points at:
        ``scope`` and ``project_id`` must match, and a ``personal_in_project``
        space must belong to ``user_id``. Anything else reads as not found.
        """
        space = self.repository.get_space(space_id)
        if (
            space is None
            or space.scope != scope
            or space.project_id != project_id
            or (scope == "personal_in_project" and space.user_id != user_id)
        ):
            raise MemorySpaceNotFoundError(f"Memory space '{space_id}' not found")
        if not space.index_s3_key:
            return ""
        return self.store.get(space.index_s3_key).decode("utf-8")

    def update_index(
        self,
        space_id: str,
        user_id: str,
        user_email: Optional[str],
        body: str,
    ) -> MemorySpace:
        """Replace the MEMORY.md index text (editor+).

        In a canonical space the index's links are checked like a file's: a
        new link to nothing fails, one it already had is tolerated.
        """
        space, _ = self._require(space_id, user_id, user_email, "editor")
        if space.file_format == "canonical":
            previous = self.store.get(space.index_s3_key).decode("utf-8") if space.index_s3_key else ""
            try:
                validate_index_links(body, self.repository.get_index(space_id).entries, previous_text=previous)
            except MemoryFormatError as exc:
                raise MemoryValidationError.from_format_error(exc) from exc
        content = self._encode(body)
        old_key = space.index_s3_key
        new_key = self.store.put(
            space_id=space_id, content=content, content_type="text/markdown"
        )
        space.index_s3_key = new_key
        space.index_content_hash = compute_content_hash(content)
        space.updated_at = _now_iso()
        self.repository.put_space(space)
        if old_key and old_key != new_key and not self._key_in_use(space_id, old_key):
            self.store.delete(old_key)
        return space

    # ---- entries -------------------------------------------------------

    def list_entries(
        self,
        space_id: str,
        user_id: str,
        user_email: Optional[str] = None,
        *,
        entry_type: Optional[EntryType] = None,
        where: Optional[Dict[str, Any]] = None,
    ) -> List[MemoryEntryRef]:
        """List manifest entries, optionally filtered by type and indexed fields.

        ``where`` matches dotted keys against each entry's ``indexed`` map by
        exact equality (operator queries like ``"<7d"`` are PR-2). This is the
        "who owes what" path — a manifest scan, never a body load.
        """
        self._require(space_id, user_id, user_email, "viewer")
        entries = self.repository.get_index(space_id).entries
        if entry_type is not None:
            entries = [e for e in entries if e.entry_type == entry_type]
        if where:
            entries = [
                e
                for e in entries
                if all(_get_nested(e.indexed, k) == v for k, v in where.items())
            ]
        return entries

    def read_entry(
        self,
        space_id: str,
        user_id: str,
        user_email: Optional[str],
        slug: str,
    ) -> str:
        self._require(space_id, user_id, user_email, "viewer")
        ref = self._find_ref(space_id, slug)
        if ref is None:
            raise MemoryEntryNotFoundError(
                f"entry '{slug}' not found in space '{space_id}'"
            )
        return self.store.get(ref.s3_key).decode("utf-8")

    def write_entry(
        self,
        space_id: str,
        user_id: str,
        user_email: Optional[str],
        slug: str,
        body: str,
        *,
        entry_type: EntryType = "fact",
        description: Optional[str] = "",
        indexed: Optional[Dict[str, Any]] = None,
        aliases: Optional[List[str]] = None,
        reason: FileVersionReason = "edit",
    ) -> MemoryEntryRef:
        """Create or replace an entry (editor+); see :meth:`save_entry`."""
        return self.save_entry(
            space_id,
            user_id,
            user_email,
            slug,
            body,
            entry_type=entry_type,
            description=description,
            indexed=indexed,
            aliases=aliases,
            reason=reason,
        ).ref

    def save_entry(
        self,
        space_id: str,
        user_id: str,
        user_email: Optional[str],
        slug: str,
        body: str,
        *,
        entry_type: EntryType = "fact",
        description: Optional[str] = None,
        indexed: Optional[Dict[str, Any]] = None,
        aliases: Optional[List[str]] = None,
        reason: FileVersionReason = "edit",
    ) -> SaveResult:
        """Create or replace an entry through the save pipeline (§4.3), editor+.

        1–4. Validate: reserved and well-formed name; in a canonical space the
             whole file (frontmatter, collisions, anchors, links).
        5.   Count tokens once (CountTokens, ~80 ms) and apply the thresholds.
        7.   Write the object, swap the manifest conditionally, then write the
             ``FILEVER`` row. The swap is the commit point: version numbers
             are assigned inside it, so they cannot collide, and a crash after
             it loses at most one history row, never content.

        Nothing is written unless every check passes. Replaced objects are
        kept: their version rows still reference them.

        ``description=None`` keeps a canonical file's description; a freeform
        entry treats it as empty, as it always has.
        """
        space, _ = self._require(space_id, user_id, user_email, "editor")
        canonical = space.file_format == "canonical"
        try:
            clean_slug = validate_slug(slug, canonical=canonical)
        except MemoryFormatError as exc:
            raise MemoryValidationError.from_format_error(exc) from exc
        if canonical:
            slug = clean_slug

        index = self.repository.get_index(space_id)
        current_ref = next((e for e in index.entries if e.slug == slug), None)
        now = _now_iso()
        validated: Optional[CanonicalSave] = None
        if canonical:
            current = self._current_file(current_ref, slug) if current_ref is not None else None
            try:
                validated = validate_canonical_save(
                    slug=slug,
                    files=index.entries,
                    current=current,
                    text=body,
                    description=description,
                    aliases=aliases,
                )
            except MemoryFormatError as exc:
                raise MemoryValidationError.from_format_error(exc) from exc
            version = _next_version(current_ref)
            created = (current.frontmatter.created if current else "") or now
            text = render_file(
                Frontmatter(
                    name=slug,
                    description=validated.description,
                    aliases=validated.aliases,
                    created=created,
                    updated=now,
                    version=version,
                ),
                validated.items,
            )
            warnings = list(validated.warnings)
        else:
            if aliases:
                raise MemoryValidationError(
                    "Aliases need a space that uses the item format.", code="aliases_unsupported"
                )
            text = body
            warnings = list(freeform_link_warnings(body, index.entries, slug=slug))

        count = self._count_tokens(text)
        hard_cap = file_hard_cap_tokens()
        over_soft = count.tokens >= file_soft_threshold_tokens()
        size_note = f"This file is about {count.tokens:,} tokens; the limit per file is {hard_cap:,}."
        if count.tokens > hard_cap:
            if canonical:
                raise MemoryValidationError(
                    f"{size_note} Split it into smaller files or remove items that are no longer needed.",
                    code="over_hard_cap",
                )
            warnings.append(size_note)
        elif over_soft:
            warnings.append(f"This file is about {count.tokens:,} tokens, close to the {hard_cap:,}-token limit.")

        content = self._encode(text)
        s3_key = self.store.put(space_id=space_id, content=content, content_type="text/markdown")
        ref = MemoryEntryRef(
            slug=slug,
            entry_type=entry_type,
            description=validated.description if validated else (description or ""),
            content_hash=compute_content_hash(content),
            size=len(content),
            s3_key=s3_key,
            updated=now,
            updated_by=user_id,
            indexed=indexed or {},
            aliases=list(validated.aliases) if validated else [],
            tokens=count.tokens,
            tokens_method=count.method,
            item_count=len(validated.items) if validated else None,
        )

        def apply(fresh_index: MemoryIndex) -> Optional[MemoryEntryRef]:
            fresh = next((e for e in fresh_index.entries if e.slug == slug), None)
            if canonical:
                # Validation read one version of this file; another save of the
                # same file since then means the anchors checked are stale.
                if (fresh is None) != (current_ref is None) or (
                    fresh is not None and current_ref is not None and fresh.content_hash != current_ref.content_hash
                ):
                    raise MemorySpaceConcurrencyError(
                        f"'{slug}' was changed by someone else while this save was in progress. "
                        "Read it again and retry."
                    )
                try:
                    check_name_collisions(
                        slug, validated.aliases, fresh_index.entries, is_new=current_ref is None
                    )
                except MemoryFormatError as exc:
                    raise MemoryValidationError.from_format_error(exc) from exc
            ref.version = _next_version(fresh)
            kept = [e for e in fresh_index.entries if e.slug != slug]
            kept.append(ref)
            kept.sort(key=lambda e: e.slug)
            fresh_index.entries = kept
            return fresh if fresh is not None and not fresh.version else None

        pre_history, _ = self._mutate_index(space_id, apply)

        if pre_history is not None:
            self._record_version(
                space_id,
                FileVersion(
                    slug=slug,
                    version=1,
                    content_hash=pre_history.content_hash,
                    size=pre_history.size,
                    updated_by=pre_history.updated_by,
                    updated_at=pre_history.updated,
                    reason="baseline",
                ),
            )
        self._record_version(
            space_id,
            FileVersion(
                slug=slug,
                version=ref.version,
                content_hash=ref.content_hash,
                size=ref.size,
                tokens=ref.tokens,
                tokens_method=ref.tokens_method,
                updated_by=user_id,
                updated_at=now,
                reason=reason,
            ),
        )
        return SaveResult(
            ref=ref,
            warnings=warnings,
            minted_anchors=list(validated.minted_anchors) if validated else [],
            removed_anchors=list(validated.removed_anchors) if validated else [],
            archived_links=list(validated.archived_links) if validated else [],
            over_soft_threshold=over_soft,
        )

    def delete_entry(
        self,
        space_id: str,
        user_id: str,
        user_email: Optional[str],
        slug: str,
    ) -> None:
        """Remove an entry, its version history and its objects (editor+).

        Deleting purges: the file's ``FILEVER`` rows go with it, and every
        object they referenced is deleted unless another entry, version or
        the index shares it (objects are content-addressed). Archive and
        restore arrive with Shared Projects 2.5.
        """
        self._require(space_id, user_id, user_email, "editor")

        def apply(index: MemoryIndex) -> List[MemoryEntryRef]:
            removed = [e for e in index.entries if e.slug == slug]
            if not removed:
                raise MemorySpaceNotFoundError(
                    f"entry '{slug}' not found in space '{space_id}'"
                )
            index.entries = [e for e in index.entries if e.slug != slug]
            return removed

        removed, final_index = self._mutate_index(space_id, apply)
        versions = self.repository.delete_file_versions(space_id, slug)
        candidates = {prev.s3_key for prev in removed}
        candidates.update(content_key(space_id, v.content_hash) for v in versions)
        still_used = self._referenced_keys(space_id, index=final_index)
        for key in candidates - still_used:
            self.store.delete(key)

    # ---- version history ------------------------------------------------

    def list_file_versions(
        self,
        space_id: str,
        user_id: str,
        user_email: Optional[str],
        slug: str,
    ) -> List[FileVersion]:
        """A file's saved versions, newest first (viewer+)."""
        self._require(space_id, user_id, user_email, "viewer")
        versions = self.repository.list_file_versions(space_id, slug)
        if not versions and self._find_ref(space_id, slug) is None:
            raise MemorySpaceNotFoundError(f"entry '{slug}' not found in space '{space_id}'")
        return sorted(versions, key=lambda v: v.version, reverse=True)

    def read_file_version(
        self,
        space_id: str,
        user_id: str,
        user_email: Optional[str],
        slug: str,
        version: int,
    ) -> Tuple[FileVersion, str]:
        """One saved version of a file and its text (viewer+)."""
        self._require(space_id, user_id, user_email, "viewer")
        row = self.repository.get_file_version(space_id, slug, version)
        if row is None:
            raise MemorySpaceNotFoundError(f"version {version} of '{slug}' not found in space '{space_id}'")
        text = self.store.get(content_key(space_id, row.content_hash)).decode("utf-8")
        return row, text

    # ---- helpers -------------------------------------------------------

    def _mutate_index(
        self, space_id: str, apply: Callable[[MemoryIndex], "_T"]
    ) -> Tuple["_T", MemoryIndex]:
        """Read-modify-conditional-write the manifest with bounded retry.

        ``apply(index)`` mutates ``index.entries`` in place and returns any
        value the caller needs afterward (e.g. the replaced refs to GC). The
        helper bumps the version and persists conditionally on the version it
        read; on a concurrent change it re-reads and re-applies, converging
        because entry writes touch a single slug. Exhausting the retries raises
        :class:`MemorySpaceConcurrencyError`. Returns ``(apply_result, final_index)``.
        """
        for attempt in range(_MAX_MANIFEST_RETRIES):
            index = self.repository.get_index(space_id)
            expected = index.version
            result = apply(index)  # may raise (e.g. NotFound) — propagate as-is
            index.version = expected + 1
            try:
                self.repository.put_index(index, expected_version=expected)
            except ManifestTooLargeError as exc:
                raise MemoryValidationError(
                    "This space has too many files for one manifest. Remove or merge some files first.",
                    code="manifest_too_large",
                ) from exc
            except OptimisticLockError:
                if attempt + 1 >= _MAX_MANIFEST_RETRIES:
                    raise MemorySpaceConcurrencyError(
                        f"memory space '{space_id}' is being edited concurrently; "
                        "retry the write"
                    )
                continue
            return result, index
        # Unreachable: the loop either returns or raises above.
        raise MemorySpaceConcurrencyError(space_id)

    def _find_ref(self, space_id: str, slug: str) -> Optional[MemoryEntryRef]:
        for ref in self.repository.get_index(space_id).entries:
            if ref.slug == slug:
                return ref
        return None

    def _key_in_use(
        self,
        space_id: str,
        s3_key: str,
        *,
        index: Optional[MemoryIndex] = None,
    ) -> bool:
        """True if an entry, a file version or the space index references ``s3_key``.

        Objects are content-addressed, so identical content under different
        slugs shares one object — never delete a key another ref still points
        at.
        """
        return s3_key in self._referenced_keys(space_id, index=index)

    def _referenced_keys(
        self,
        space_id: str,
        *,
        index: Optional[MemoryIndex] = None,
        space: Optional[MemorySpace] = None,
    ) -> set[str]:
        """Every object key the space still needs: entries, versions, index."""
        idx = index if index is not None else self.repository.get_index(space_id)
        keys = {e.s3_key for e in idx.entries}
        keys.update(
            content_key(space_id, v.content_hash) for v in self.repository.list_file_versions(space_id)
        )
        current = space if space is not None else self.repository.get_space(space_id)
        if current is not None and current.index_s3_key:
            keys.add(current.index_s3_key)
        return keys

    def _current_file(self, ref: MemoryEntryRef, slug: str) -> CurrentFile:
        """Parse the stored version of a canonical file."""
        parsed = parse_file(self.store.get(ref.s3_key).decode("utf-8"))
        return CurrentFile(
            frontmatter=frontmatter_from_parsed(parsed.frontmatter or {}, name=slug),
            items=parsed.items,
        )

    def _record_version(self, space_id: str, version: FileVersion) -> None:
        """Write a ``FILEVER`` row after its commit; a failure loses history, not content."""
        try:
            self.repository.put_file_version(space_id, version)
        except Exception:  # noqa: BLE001 - the save itself already committed
            logger.warning(
                "memory-spaces: could not record version %d of slug in space=%s",
                version.version,
                space_id,
                exc_info=True,
            )

    @staticmethod
    def _encode(text: str) -> bytes:
        try:
            return text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise MemoryValidationError(
                "The text contains characters that cannot be stored (invalid Unicode).",
                code="invalid_text",
            ) from exc

    def _touch(self, space_id: str) -> None:
        space = self.repository.get_space(space_id)
        if space is not None:
            space.updated_at = _now_iso()
            self.repository.put_space(space)
