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
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, Tuple, TypeVar

from .format import (
    Frontmatter,
    MemoryFormatError,
    extract_links,
    frontmatter_from_parsed,
    Item,
    new_anchor,
    parse_file,
    render_file,
    render_items,
    strip_anchors,
    validate_slug,
)
from .models import (
    ArchivedItem,
    EntryType,
    FileFormat,
    FileVersion,
    FileVersionReason,
    ItemProvenance,
    MaintenanceOp,
    MaintenanceVerification,
    MemoryEntryRef,
    MemoryIndex,
    MemoryProposal,
    MemoryScope,
    MemorySpace,
    ProposalState,
    ProposerKind,
    Role,
    ShareRole,
    SpaceMember,
)
from .maintenance.ops import CompactionResult, apply_ops
from .repository import ManifestTooLargeError, MemorySpaceRepository, OptimisticLockError
from .store import (
    MemorySpaceStore,
    MemorySpaceStoreError,
    compute_content_hash,
    content_key,
    get_memory_space_store,
)
from .templates import DEFAULT_TEMPLATE_ID, get_template, is_valid_template
from .tokens import TokenCount, count_file_tokens, estimate_tokens
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


def archive_retention_days(scope: str) -> int:
    """How long an archived item stays restorable: a project's shared memory keeps it
    ``MEMORY_ARCHIVE_RETENTION_DAYS`` (365), anything else
    ``MEMORY_PERSONAL_ARCHIVE_RETENTION_DAYS`` (30, an undo window)."""
    if scope == "shared":
        return _env_int("MEMORY_ARCHIVE_RETENTION_DAYS", 365, minimum=1)
    return _env_int("MEMORY_PERSONAL_ARCHIVE_RETENTION_DAYS", 30, minimum=1)


def _normalize_email(email: Optional[str]) -> str:
    return (email or "").strip().lower()


@dataclass
class SaveContext:
    """Where a save came from, for its items' provenance (Shared Projects 2.5a-2).

    ``restored`` maps an anchor coming back from the archive to the provenance
    it left with; everything else describes this save.
    """

    source_session_id: Optional[str] = None
    proposal_id: Optional[str] = None
    proposed_by: Optional[str] = None
    approved_by: Optional[str] = None
    restored: Dict[str, ItemProvenance] = field(default_factory=dict)
    # Maintenance (2.6): why each anchor leaving the file left, and what replaced it.
    archive_reasons: Dict[str, Tuple[str, Optional[str]]] = field(default_factory=dict)


def max_pending_proposals() -> int:
    """Pending proposals a space may hold (``MEMORY_MAX_PENDING_PROPOSALS``, default 100)."""
    return _env_int("MEMORY_MAX_PENDING_PROPOSALS", 100, minimum=1)


# A reviewer's note on a decision, like a share's hand-off note.
MAX_PROPOSAL_NOTE_CHARS = 280


def _new_proposal_id() -> str:
    """Sorts by creation time, so a space's ``PROPOSAL#`` rows list oldest first."""
    return f"{int(datetime.now(timezone.utc).timestamp() * 1000):013d}-{uuid.uuid4().hex[:8]}"


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


IndexLinkOutcome = Literal["added", "already_linked", "over_budget"]


def _index_line(slug: str, description: str) -> str:
    """One canonical index line: ``- [[slug]] — description``, or just the link."""
    summary = " ".join((description or "").split())
    return f"- [[{slug}]] — {summary}" if summary else f"- [[{slug}]]"


def _without_starter(index_text: str, template: str) -> str:
    """The index with an untouched one-heading starter (the blank template's) reduced to that heading.

    A starter with sections (chief of staff, research notebook) is structure
    worth keeping, so it is left as it is.
    """
    if not is_valid_template(template):
        return index_text
    lines = get_template(template).starter_index.strip().splitlines()
    if index_text.strip() != "\n".join(lines) or sum(line.startswith("#") for line in lines) != 1:
        return index_text
    return f"{lines[0]}\n"


def _append_index_line(index_text: str, line: str) -> str:
    """``line`` after the index's text: in its list if it ends with one, else after a blank line."""
    body = index_text.rstrip()
    if not body:
        return f"{line}\n"
    separator = "\n" if body.splitlines()[-1].lstrip().startswith("- ") else "\n\n"
    return f"{body}{separator}{line}\n"


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


class MemoryProposalStateError(MemorySpaceError):
    """The proposal was already decided or withdrawn (409), or there are too many pending."""


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
class PreparedSave:
    """A save that passed every check (§4.3 steps 1–5) and has not been written yet."""

    slug: str
    text: str
    current_ref: Optional[MemoryEntryRef]
    validated: Optional[CanonicalSave]
    current_items: Tuple[Item, ...]
    warnings: List[str]
    count: TokenCount
    over_soft: bool


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


def _clean_note(note: Optional[str]) -> Optional[str]:
    text = " ".join((note or "").split())
    if len(text) > MAX_PROPOSAL_NOTE_CHARS:
        raise MemoryValidationError(
            f"A note can be at most {MAX_PROPOSAL_NOTE_CHARS} characters.", code="note_too_long"
        )
    return text or None


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


def _check_pins_kept(current_ref: Optional[MemoryEntryRef], validated: CanonicalSave) -> None:
    pinned = set(current_ref.pinned) if current_ref is not None else set()
    dropped = sorted(pinned & set(validated.removed_anchors))
    if dropped:
        raise MemoryValidationError(
            f"{'An item' if len(dropped) == 1 else f'{len(dropped)} items'} this save leaves out "
            f"{'is' if len(dropped) == 1 else 'are'} pinned ({', '.join(dropped)}). "
            "Keep pinned items in the file. Only a person can unpin one (an editor, for "
            "project memory), so ask them to if it should go.",
            code="pinned_item_removed",
        )


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

    def add_index_link(
        self,
        space_id: str,
        user_id: str,
        user_email: Optional[str],
        ref: MemoryEntryRef,
        *,
        max_tokens: int,
    ) -> IndexLinkOutcome:
        """Append ``- [[slug]] — description`` to ``MEMORY.md`` for a file it doesn't link yet (editor+).

        For a project harness's ``memory_save`` of a new file: only the index is
        injected into a turn, and a model told to index its own saves usually
        forgets (the 2026-10 team simulation, G2). Nothing is written when:

        - the index already links the file by name or alias (``already_linked``);
        - the line would push the index past ``max_tokens``, the budget it is
          injected under, where it would be truncated away (``over_budget``).
          Estimated exactly as the injection does, after anchors are stripped.

        The line is appended last, the index's other text kept byte for byte.
        A template starter with nothing written under it is replaced by its
        heading: its placeholder prose would otherwise be injected every turn.
        The write is conditional on the index the line was added to, so two
        members creating files at once keep both lines; the index is not
        versioned, so no ``FILEVER`` row is written.
        """
        space: Optional[MemorySpace]
        space, _ = self._require(space_id, user_id, user_email, "editor")
        names = {ref.slug.casefold(), *(a.casefold() for a in ref.aliases)}
        line = _index_line(ref.slug, ref.description)
        for attempt in range(_MAX_MANIFEST_RETRIES):
            if attempt:
                space = self.repository.get_space(space_id)
            if space is None:
                raise MemorySpaceNotFoundError(f"Memory space '{space_id}' not found")
            previous = self.store.get(space.index_s3_key).decode("utf-8") if space.index_s3_key else ""
            if any(name.casefold() in names for name in extract_links(previous)):
                return "already_linked"
            text = _append_index_line(_without_starter(previous, space.template), line)
            if estimate_tokens(strip_anchors(text)) > max_tokens:
                return "over_budget"
            content = self._encode(text)
            old_key, old_hash = space.index_s3_key, space.index_content_hash
            new_key = self.store.put(space_id=space_id, content=content, content_type="text/markdown")
            space.index_s3_key = new_key
            space.index_content_hash = compute_content_hash(content)
            space.updated_at = _now_iso()
            try:
                self.repository.put_space_if_index_unchanged(space, old_hash)
            except OptimisticLockError:
                if new_key != old_key and not self._key_in_use(space_id, new_key):
                    self.store.delete(new_key)
                continue
            if old_key and old_key != new_key and not self._key_in_use(space_id, old_key):
                self.store.delete(old_key)
            return "added"
        raise MemorySpaceConcurrencyError(
            f"the index of memory space '{space_id}' is being edited concurrently; retry the write"
        )

    def remove_index_link(self, space_id: str, user_id: str, user_email: Optional[str], slug: str) -> bool:
        """Drop ``MEMORY.md``'s own line for a deleted file (editor+). Returns whether one was removed.

        Only a list line that starts with ``[[slug]]`` (the line ``add_index_link``
        writes, or one shaped like it) goes; a mention anywhere else stays, as a
        dead link the next index save warns about. Without this a deleted file
        kept its line in the block every task loads, and the assistant went
        looking for it. The write is conditional on the index it read, like
        ``add_index_link``.
        """
        space: Optional[MemorySpace]
        space, _ = self._require(space_id, user_id, user_email, "editor")
        pattern = re.compile(r"^\s*[-*]\s+\[\[\s*" + re.escape(slug) + r"\s*\]\]", re.IGNORECASE)
        for attempt in range(_MAX_MANIFEST_RETRIES):
            if attempt:
                space = self.repository.get_space(space_id)
            if space is None:
                raise MemorySpaceNotFoundError(f"Memory space '{space_id}' not found")
            previous = self.store.get(space.index_s3_key).decode("utf-8") if space.index_s3_key else ""
            lines = previous.split("\n")
            kept = [line for line in lines if not pattern.match(line)]
            if len(kept) == len(lines):
                return False
            content = self._encode("\n".join(kept))
            old_key, old_hash = space.index_s3_key, space.index_content_hash
            new_key = self.store.put(space_id=space_id, content=content, content_type="text/markdown")
            space.index_s3_key = new_key
            space.index_content_hash = compute_content_hash(content)
            space.updated_at = _now_iso()
            try:
                self.repository.put_space_if_index_unchanged(space, old_hash)
            except OptimisticLockError:
                if new_key != old_key and not self._key_in_use(space_id, new_key):
                    self.store.delete(new_key)
                continue
            if old_key and old_key != new_key and not self._key_in_use(space_id, old_key):
                self.store.delete(old_key)
            return True
        raise MemorySpaceConcurrencyError(
            f"the index of memory space '{space_id}' is being edited concurrently; retry the write"
        )

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
        proposal_id: Optional[str] = None,
        context: Optional[SaveContext] = None,
        restorable: Sequence[str] = (),
        run_id: Optional[str] = None,
        base_content_hash: Optional[str] = None,
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
        entry treats it as empty, as it always has. ``base_content_hash`` is the
        version ``body`` was derived from; a file that has moved on since is a
        conflict rather than a silent overwrite.
        """
        space, _ = self._require(space_id, user_id, user_email, "editor")
        canonical = space.file_format == "canonical"
        now = _now_iso()
        prepared = self._prepare_save(
            space, slug, body, description=description, aliases=aliases, now=now, restorable=restorable
        )
        if base_content_hash is not None and (
            prepared.current_ref is None or prepared.current_ref.content_hash != base_content_hash
        ):
            raise MemorySpaceConcurrencyError(
                f"'{slug}' was changed by someone else while this save was in progress. Read it again and retry."
            )
        result = self._commit_save(
            space_id, user_id, prepared, canonical=canonical, entry_type=entry_type,
            description=description, indexed=indexed, now=now, reason=reason, proposal_id=proposal_id,
            run_id=run_id,
        )
        if canonical:
            self._record_items(space, prepared, result, actor=_normalize_email(user_email), now=now, context=context)
        return result

    def _prepare_save(
        self,
        space: MemorySpace,
        slug: str,
        body: str,
        *,
        description: Optional[str],
        aliases: Optional[List[str]],
        now: str,
        restorable: Sequence[str] = (),
    ) -> PreparedSave:
        """§4.3 steps 1–5 against the current manifest: validate, render, count. Writes nothing.

        A save may not drop a pinned item (2.5a-2): the pin is how a member says
        "keep this", so dropping it takes an unpin first.
        """
        space_id = space.space_id
        canonical = space.file_format == "canonical"
        try:
            clean_slug = validate_slug(slug, canonical=canonical)
        except MemoryFormatError as exc:
            raise MemoryValidationError.from_format_error(exc) from exc
        if canonical:
            slug = clean_slug

        index = self.repository.get_index(space_id)
        current_ref = next((e for e in index.entries if e.slug == slug), None)
        validated: Optional[CanonicalSave] = None
        current_items: Tuple[Item, ...] = ()
        if canonical:
            current = self._current_file(current_ref, slug) if current_ref is not None else None
            current_items = current.items if current else ()
            try:
                validated = validate_canonical_save(
                    slug=slug,
                    files=index.entries,
                    current=current,
                    text=body,
                    description=description,
                    aliases=aliases,
                    restorable=restorable,
                )
            except MemoryFormatError as exc:
                raise MemoryValidationError.from_format_error(exc) from exc
            _check_pins_kept(current_ref, validated)
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
        return PreparedSave(
            slug=slug, text=text, current_ref=current_ref, validated=validated, current_items=current_items,
            warnings=warnings, count=count, over_soft=over_soft,
        )

    def _commit_save(
        self,
        space_id: str,
        user_id: str,
        prepared: PreparedSave,
        *,
        canonical: bool,
        entry_type: EntryType,
        description: Optional[str],
        indexed: Optional[Dict[str, Any]],
        now: str,
        reason: FileVersionReason,
        proposal_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> SaveResult:
        """§4.3 step 7: write the object, swap the manifest conditionally, then write ``FILEVER``."""
        slug, text, current_ref = prepared.slug, prepared.text, prepared.current_ref
        validated, warnings, count, over_soft = prepared.validated, prepared.warnings, prepared.count, prepared.over_soft
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
                # Pins live on the manifest entry, which this save replaces: carry them.
                ref.pinned = list(fresh.pinned) if fresh is not None else []
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
                proposal_id=proposal_id,
                run_id=run_id,
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
        space, _ = self._require(space_id, user_id, user_email, "editor")
        leaving: List[Tuple[str, str, Optional[ItemProvenance]]] = []
        if space.file_format == "canonical":
            # Its items go to the archive (2.5a-2) before history and objects are purged.
            ref = self._find_ref(space_id, slug)
            if ref is not None:
                try:
                    provenance = self.repository.get_provenance(space_id, slug)
                    leaving = [
                        (i.anchor, i.text, provenance.get(i.anchor))
                        for i in self._current_file(ref, slug).items if i.anchor
                    ]
                except Exception:
                    logger.warning("Could not read '%s' to archive it in %s", slug, space_id, exc_info=True)

        def apply(index: MemoryIndex) -> List[MemoryEntryRef]:
            removed = [e for e in index.entries if e.slug == slug]
            if not removed:
                raise MemorySpaceNotFoundError(
                    f"entry '{slug}' not found in space '{space_id}'"
                )
            index.entries = [e for e in index.entries if e.slug != slug]
            return removed

        removed, final_index = self._mutate_index(space_id, apply)
        if leaving:
            try:
                self._archive(space, slug, leaving, reason="deleted", actor=_normalize_email(user_email), now=_now_iso())
                self.repository.delete_provenance(space_id, slug)
            except Exception:
                logger.warning("Could not archive deleted file '%s' in %s", slug, space_id, exc_info=True)
        versions = self.repository.delete_file_versions(space_id, slug)
        try:
            self.repository.delete_retrieval_stats(space_id, slug)
        except Exception:
            logger.warning("Could not clear the read counts of deleted file '%s' in %s", slug, space_id, exc_info=True)
        candidates = {prev.s3_key for prev in removed}
        candidates.update(content_key(space_id, v.content_hash) for v in versions)
        still_used = self._referenced_keys(space_id, index=final_index)
        for key in candidates - still_used:
            self.store.delete(key)

    # ---- item provenance, archive and pins (Shared Projects 2.5a-2) -------

    def _record_items(
        self,
        space: MemorySpace,
        prepared: PreparedSave,
        result: SaveResult,
        *,
        actor: str,
        now: str,
        context: Optional[SaveContext],
    ) -> None:
        """After a canonical save commits: update each item's provenance, archive what left.

        Best-effort, like the ``FILEVER`` row: the save has committed, and a lost
        provenance write costs attribution, never content (the archive row's
        text also survives in history).
        """
        ctx = context or SaveContext()
        validated = prepared.validated
        if validated is None:
            return
        space_id, slug = space.space_id, prepared.slug
        try:
            before = self.repository.get_provenance(space_id, slug)
            old_text = {i.anchor: i.text for i in prepared.current_items if i.anchor}
            minted = set(validated.minted_anchors)
            stamp = {
                "source_session_id": ctx.source_session_id,
                "proposal_id": ctx.proposal_id,
                "proposed_by": ctx.proposed_by,
                "approved_by": ctx.approved_by,
            }
            after: Dict[str, ItemProvenance] = {}
            for item in validated.items:
                anchor = item.anchor or ""
                kept = before.get(anchor)
                if anchor in ctx.restored:
                    after[anchor] = ctx.restored[anchor].model_copy(update={"restored_by": actor, "restored_at": now})
                elif anchor in minted:
                    after[anchor] = ItemProvenance(added_by=actor, added_at=now, **stamp)
                elif old_text.get(anchor) != item.text:
                    base = kept or ItemProvenance()
                    after[anchor] = base.model_copy(update={"updated_by": actor, "updated_at": now, **stamp})
                elif kept is not None:
                    after[anchor] = kept
            self.repository.put_provenance(space_id, slug, after)
            leaving = [
                (a, old_text[a], before.get(a)) for a in validated.removed_anchors if a in old_text
            ]
            self._archive(
                space, slug, leaving, reason="removed", actor=actor, now=now, reasons=ctx.archive_reasons
            )
        except Exception:
            logger.warning("Could not record item provenance for '%s' in space %s", slug, space_id, exc_info=True)

    def _archive(
        self,
        space: MemorySpace,
        slug: str,
        leaving: List[Tuple[str, str, Optional[ItemProvenance]]],
        *,
        reason: str,
        actor: str,
        now: str,
        reasons: Optional[Dict[str, Tuple[str, Optional[str]]]] = None,
    ) -> None:
        """Archive ``leaving`` items. ``reasons`` overrides ``reason`` per anchor, with what replaced it."""
        if not leaving:
            return
        reasons = reasons or {}
        days = archive_retention_days(space.scope)
        moment = datetime.now(timezone.utc)
        until = moment + timedelta(days=days)
        stamp = f"{int(moment.timestamp() * 1000):013d}"
        rows = [
            ArchivedItem(
                archive_id=f"{stamp}-{anchor}",
                slug=slug,
                anchor=anchor,
                text=text,
                reason=reasons.get(anchor, (reason, None))[0],
                archived_by=actor,
                archived_at=now,
                restorable_until=until.isoformat(),
                provenance=prov,
                superseded_by=reasons.get(anchor, (reason, None))[1],
            )
            for anchor, text, prov in leaving
        ]
        self.repository.put_archived_items(space.space_id, rows, ttl=int(until.timestamp()))

    def read_file_items(
        self, space_id: str, user_id: str, user_email: Optional[str], slug: str
    ) -> Tuple[MemoryEntryRef, Tuple[Item, ...], Dict[str, ItemProvenance]]:
        """A canonical file as structured items, with its pins (on the ref) and provenance (viewer+)."""
        space, _ = self._require(space_id, user_id, user_email, "viewer")
        ref = self._find_ref(space_id, slug)
        if ref is None:
            raise MemoryEntryNotFoundError(f"entry '{slug}' not found in space '{space_id}'")
        if space.file_format != "canonical":
            raise MemoryValidationError("This space doesn't use the item format.", code="not_canonical")
        current = self._current_file(ref, slug)
        return ref, current.items, self.repository.get_provenance(space_id, slug)

    def set_pinned(
        self, space_id: str, user_id: str, user_email: Optional[str], slug: str, anchor: str, *, pinned: bool
    ) -> List[str]:
        """Pin or unpin one item (editor+). Returns the file's pinned anchors."""
        space, _ = self._require(space_id, user_id, user_email, "editor")
        if space.file_format != "canonical":
            raise MemoryValidationError("Only item-format files have pins.", code="not_canonical")
        anchor = (anchor or "").strip().lower()
        ref = self._find_ref(space_id, slug)
        if ref is None:
            raise MemoryEntryNotFoundError(f"entry '{slug}' not found in space '{space_id}'")
        if pinned and anchor not in {i.anchor for i in self._current_file(ref, slug).items}:
            raise MemoryEntryNotFoundError(f"'{slug}' has no item {anchor}")

        def apply(index: MemoryIndex) -> List[str]:
            fresh = next((e for e in index.entries if e.slug == slug), None)
            if fresh is None:
                raise MemoryEntryNotFoundError(f"entry '{slug}' not found in space '{space_id}'")
            if pinned and fresh.content_hash != ref.content_hash:
                raise MemorySpaceConcurrencyError(f"'{slug}' changed while pinning. Read it again and retry.")
            current = set(fresh.pinned)
            current = current | {anchor} if pinned else current - {anchor}
            fresh.pinned = sorted(current)
            return fresh.pinned

        result, _ = self._mutate_index(space_id, apply)
        return result

    def list_archived_items(self, space_id: str, user_id: str, user_email: Optional[str]) -> List[ArchivedItem]:
        """Restorable items, newest first (viewer+). Expired rows DynamoDB hasn't removed yet are left out."""
        self._require(space_id, user_id, user_email, "viewer")
        now = _now_iso()
        rows = [a for a in self.repository.list_archived_items(space_id) if a.restorable_until > now]
        return sorted(rows, key=lambda a: a.archive_id, reverse=True)

    def restore_archived_item(
        self, space_id: str, user_id: str, user_email: Optional[str], archive_id: str
    ) -> SaveResult:
        """Put an archived item back at the end of its file (editor+), keeping its anchor and provenance.

        A file deleted since is created again. The restore is an ordinary save
        (``reason: restore``), so it is validated, versioned and counted like
        any other; the archive row goes once it has committed.
        """
        space, _ = self._require(space_id, user_id, user_email, "editor")
        row = self.repository.get_archived_item(space_id, archive_id)
        if row is None or row.restorable_until <= _now_iso():
            raise MemorySpaceNotFoundError(f"archived item '{archive_id}' not found in space '{space_id}'")
        ref = self._find_ref(space_id, row.slug)
        current_items = self._current_file(ref, row.slug).items if ref is not None else ()
        # An anchor the file has since minted again comes back under a fresh one.
        taken = {i.anchor for i in current_items}
        anchor = row.anchor
        while anchor in taken:
            anchor = new_anchor()
        body = render_items([*current_items, Item(text=row.text, anchor=anchor)])
        result = self.save_entry(
            space_id, user_id, user_email, row.slug, body,
            reason="restore",
            restorable=[anchor],
            context=SaveContext(restored={anchor: row.provenance or ItemProvenance(added_by=row.archived_by)}),
        )
        try:
            self.repository.delete_archived_item(space_id, archive_id)
        except Exception:
            logger.warning("Could not clear restored archive row %s in %s", archive_id, space_id, exc_info=True)
        return result

    # ---- proposals (Shared Projects 2.5a) ---------------------------------

    def create_proposal(
        self,
        space_id: str,
        user_id: str,
        user_email: Optional[str],
        slug: str,
        text: str,
        *,
        description: Optional[str] = None,
        aliases: Optional[List[str]] = None,
        proposer_kind: ProposerKind = "member",
        source_session_id: Optional[str] = None,
    ) -> Tuple[MemoryProposal, List[str]]:
        """Queue a change to a project's shared memory for an editor (viewer+).

        The text goes through the same checks as a save (§4.3 steps 1–5), so a
        proposal an editor can't approve is refused now, while the proposer can
        still fix it. What is stored is the proposer's text, not its render: an
        approval runs the pipeline again against the file as it is by then.
        Returns the proposal and the save's warnings.
        """
        space, _ = self._require(space_id, user_id, user_email, "viewer")
        if space.scope != "shared":
            raise MemoryValidationError(
                "Proposals are for a project's shared memory.", code="proposals_unsupported"
            )
        pending = [p for p in self.repository.list_proposals(space_id) if p.state == "pending"]
        if len(pending) >= max_pending_proposals():
            raise MemoryProposalStateError(
                f"This project already has {len(pending)} changes waiting for review. "
                "Ask an editor to review them before proposing more."
            )
        now = _now_iso()
        prepared = self._prepare_save(space, slug, text, description=description, aliases=aliases, now=now)
        base = prepared.current_ref
        proposal = MemoryProposal(
            proposal_id=_new_proposal_id(),
            slug=prepared.slug,
            text=text,
            description=description,
            aliases=aliases,
            base_version=base.version if base else 0,
            base_content_hash=base.content_hash if base else "",
            tokens=prepared.count.tokens,
            proposer_id=user_id,
            proposer_email=(user_email or "").strip().lower(),
            proposer_kind=proposer_kind,
            source_session_id=source_session_id,
            created_at=now,
        )
        self.repository.put_proposal(space_id, proposal)
        return proposal, list(prepared.warnings)

    def list_proposals(
        self,
        space_id: str,
        user_id: str,
        user_email: Optional[str],
        *,
        state: Optional[ProposalState] = None,
    ) -> List[MemoryProposal]:
        """Newest first. Editors see every proposal; anyone else sees their own."""
        _, role = self._require(space_id, user_id, user_email, "viewer")
        proposals = self.repository.list_proposals(space_id)
        if _ROLE_RANK[role] < _ROLE_RANK["editor"]:
            proposals = [p for p in proposals if p.proposer_id == user_id]
        if state is not None:
            proposals = [p for p in proposals if p.state == state]
        return sorted(proposals, key=lambda p: p.proposal_id, reverse=True)

    def get_proposal(
        self, space_id: str, user_id: str, user_email: Optional[str], proposal_id: str
    ) -> MemoryProposal:
        """One proposal, to an editor or its proposer. Anyone else gets not-found."""
        _, role = self._require(space_id, user_id, user_email, "viewer")
        proposal = self.repository.get_proposal(space_id, proposal_id)
        if proposal is None or (_ROLE_RANK[role] < _ROLE_RANK["editor"] and proposal.proposer_id != user_id):
            raise MemorySpaceNotFoundError(f"proposal '{proposal_id}' not found in space '{space_id}'")
        return proposal

    def proposal_is_stale(self, space_id: str, proposal: MemoryProposal) -> bool:
        """Whether the file changed (or appeared, or went) since the proposal was written."""
        current = self._find_ref(space_id, proposal.slug)
        return (current.content_hash if current else "") != proposal.base_content_hash

    def approve_proposal(
        self,
        space_id: str,
        user_id: str,
        user_email: Optional[str],
        proposal_id: str,
        *,
        text: Optional[str] = None,
        description: Optional[str] = None,
        note: Optional[str] = None,
        ops: Optional[Sequence[int]] = None,
    ) -> Tuple[MemoryProposal, SaveResult]:
        """Apply a pending proposal as a save (``reason: proposal``), editor+.

        ``text`` is the reviewer's edited version. Without one, a proposal whose
        file has changed since it was written is refused (409): applying it would
        silently undo the newer edit. The proposal is claimed (pending →
        approved) before the save, so two reviewers can't both apply it, and
        put back to pending if the save fails.

        A compaction proposal (2.6) without ``text`` applies its ``ops`` (all, or
        the indexes given) to the file as it is now; see :meth:`_approve_compaction`.
        """
        self._require(space_id, user_id, user_email, "editor")
        proposal = self.get_proposal(space_id, user_id, user_email, proposal_id)
        self._require_pending(proposal)
        if ops is not None and (proposal.kind != "compaction" or text is not None):
            raise MemoryValidationError(
                "Choosing changes is for maintenance proposals approved as proposed.", code="ops_unsupported"
            )
        if proposal.kind == "compaction" and text is None:
            return self._approve_compaction(space_id, user_id, user_email, proposal, ops=ops, note=note)
        if text is None and self.proposal_is_stale(space_id, proposal):
            raise MemorySpaceConcurrencyError(
                f"'{proposal.slug}' has changed since this was proposed. "
                "Review it against the current file and approve an edited version, or reject it."
            )
        decided = proposal.model_copy(update={
            "state": "approved",
            "decided_by": user_id,
            "decided_by_email": (user_email or "").strip().lower(),
            "decided_at": _now_iso(),
            "note": _clean_note(note),
            "edited": text is not None,
        })
        self._transition(space_id, decided, expected="pending")
        try:
            result = self.save_entry(
                space_id, user_id, user_email, proposal.slug, text if text is not None else proposal.text,
                description=description if description is not None else proposal.description,
                aliases=proposal.aliases,
                reason="proposal",
                proposal_id=proposal.proposal_id,
                context=SaveContext(
                    source_session_id=proposal.source_session_id,
                    proposal_id=proposal.proposal_id,
                    proposed_by=proposal.proposer_email or None,
                    approved_by=_normalize_email(user_email) or None,
                ),
            )
        except Exception:
            self._transition(space_id, proposal, expected="approved")
            raise
        decided.result_version = result.ref.version
        try:
            self.repository.put_proposal(space_id, decided)
        except Exception:
            logger.warning("Could not record the version proposal %s wrote", proposal_id, exc_info=True)
        return decided, result

    def reject_proposal(
        self, space_id: str, user_id: str, user_email: Optional[str], proposal_id: str, *, note: Optional[str] = None
    ) -> MemoryProposal:
        """Decline a pending proposal (editor+). Nothing is written to the file."""
        self._require(space_id, user_id, user_email, "editor")
        proposal = self.get_proposal(space_id, user_id, user_email, proposal_id)
        self._require_pending(proposal)
        decided = proposal.model_copy(update={
            "state": "rejected",
            "decided_by": user_id,
            "decided_by_email": (user_email or "").strip().lower(),
            "decided_at": _now_iso(),
            "note": _clean_note(note),
        })
        self._transition(space_id, decided, expected="pending")
        return decided

    def withdraw_proposal(
        self, space_id: str, user_id: str, user_email: Optional[str], proposal_id: str
    ) -> MemoryProposal:
        """The proposer takes back their own pending proposal."""
        self._require(space_id, user_id, user_email, "viewer")
        proposal = self.repository.get_proposal(space_id, proposal_id)
        if proposal is None or proposal.proposer_id != user_id:
            raise MemorySpaceNotFoundError(f"proposal '{proposal_id}' not found in space '{space_id}'")
        self._require_pending(proposal)
        withdrawn = proposal.model_copy(update={"state": "withdrawn", "decided_at": _now_iso()})
        self._transition(space_id, withdrawn, expected="pending")
        return withdrawn

    # ---- maintenance proposals (Shared Projects 2.6) ----------------------

    def create_compaction_proposal(
        self,
        space_id: str,
        *,
        requester_id: str,
        requester_email: str,
        slug: str,
        ops: List[MaintenanceOp],
        verification: MaintenanceVerification,
        run_id: str,
        base: MemoryEntryRef,
        items: Sequence[Item],
    ) -> MemoryProposal:
        """Queue a maintenance run's changes to one file, in the name of the member who started it.

        Called by the maintenance worker, which holds no user session: the
        member's editor role was checked when they started the run. The result
        (``items``) is checked like any save first, so a reviewer is never shown
        a change that can't be applied. A file takes one maintenance proposal at
        a time, and the space's pending cap applies.
        """
        space = self.repository.get_space(space_id)
        if space is None:
            raise MemorySpaceNotFoundError(f"memory space '{space_id}' not found")
        pending = [p for p in self.repository.list_proposals(space_id) if p.state == "pending"]
        if any(p.kind == "compaction" and p.slug == slug for p in pending):
            raise MemoryProposalStateError(f"'{slug}' already has maintenance changes waiting for review.")
        if len(pending) >= max_pending_proposals():
            raise MemoryProposalStateError(f"This project already has {len(pending)} changes waiting for review.")
        body = render_items(items)
        prepared = self._prepare_save(space, slug, body, description=None, aliases=None, now=_now_iso())
        proposal = MemoryProposal(
            proposal_id=_new_proposal_id(),
            kind="compaction",
            slug=slug,
            text=body,
            base_version=base.version,
            base_content_hash=base.content_hash,
            tokens=prepared.count.tokens,
            proposer_id=requester_id,
            proposer_email=_normalize_email(requester_email),
            proposer_kind="maintenance",
            created_at=_now_iso(),
            ops=ops,
            verification=verification,
            run_id=run_id,
        )
        self.repository.put_proposal(space_id, proposal)
        return proposal

    def preview_compaction(
        self, space_id: str, proposal: MemoryProposal, *, selected: Optional[Sequence[int]] = None
    ) -> Tuple[CompactionResult, Optional[MemoryEntryRef]]:
        """A compaction proposal's ops applied to the file as it is now. Reads the file."""
        ref = self._find_ref(space_id, proposal.slug)
        if ref is None:
            ops = proposal.ops or []
            skipped = list(range(len(ops))) if selected is None else list(selected)
            return CompactionResult(items=(), skipped=skipped), None
        items = self._current_file(ref, proposal.slug).items
        return apply_ops(items, proposal.ops or [], pinned=ref.pinned, selected=selected), ref

    def compaction_view(self, space_id: str, proposal: MemoryProposal) -> Tuple[str, bool]:
        """``(text, stale)`` for a pending compaction proposal, as a reviewer should see it.

        While the file is unchanged that is the stored text. Once it has
        changed, the text is recomputed against the current file, and the
        proposal is stale only if none of its changes still apply.
        """
        if not self.proposal_is_stale(space_id, proposal):
            return proposal.text, False
        result, ref = self.preview_compaction(space_id, proposal)
        if ref is None or not result.applied:
            return proposal.text, True
        return render_items(result.items), False

    def _approve_compaction(
        self,
        space_id: str,
        user_id: str,
        user_email: Optional[str],
        proposal: MemoryProposal,
        *,
        ops: Optional[Sequence[int]],
        note: Optional[str],
    ) -> Tuple[MemoryProposal, SaveResult]:
        """Apply the chosen ops to the file as it is now (``reason: maintenance``).

        Ops are addressed by anchor, so an edit since the run doesn't block the
        rest: an op whose items changed, went or got pinned is skipped, and
        ``applied_ops`` records what went in. None left to apply is a 409.
        """
        total = len(proposal.ops or [])
        if ops is not None and (not ops or any(i < 0 or i >= total for i in ops)):
            raise MemoryValidationError(
                f"Choose one or more changes, numbered 0 to {total - 1}.", code="ops_invalid"
            )
        result, ref = self.preview_compaction(space_id, proposal, selected=ops)
        if ref is None or not result.applied:
            raise MemorySpaceConcurrencyError(
                f"'{proposal.slug}' has changed since maintenance ran, and none of these changes still apply. "
                "Decline it, and run maintenance again if the file still needs it."
            )
        decided = proposal.model_copy(update={
            "state": "approved",
            "decided_by": user_id,
            "decided_by_email": _normalize_email(user_email),
            "decided_at": _now_iso(),
            "note": _clean_note(note),
            "applied_ops": result.applied,
        })
        self._transition(space_id, decided, expected="pending")
        try:
            saved = self.save_entry(
                space_id, user_id, user_email, proposal.slug, render_items(result.items),
                reason="maintenance",
                proposal_id=proposal.proposal_id,
                run_id=proposal.run_id,
                base_content_hash=ref.content_hash,
                context=SaveContext(
                    proposal_id=proposal.proposal_id,
                    proposed_by=proposal.proposer_email or None,
                    approved_by=_normalize_email(user_email) or None,
                    archive_reasons=result.archive,
                ),
            )
        except Exception:
            self._transition(space_id, proposal, expected="approved")
            raise
        decided.result_version = saved.ref.version
        try:
            self.repository.put_proposal(space_id, decided)
        except Exception:
            logger.warning("Could not record the version proposal %s wrote", proposal.proposal_id, exc_info=True)
        return decided, saved

    @staticmethod
    def _require_pending(proposal: MemoryProposal) -> None:
        if proposal.state != "pending":
            raise MemoryProposalStateError(f"This proposal was already {proposal.state}.")

    def _transition(self, space_id: str, proposal: MemoryProposal, *, expected: str) -> None:
        try:
            self.repository.put_proposal(space_id, proposal, expected_state=expected)
        except OptimisticLockError as exc:
            raise MemoryProposalStateError("Someone else decided this proposal first.") from exc

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
