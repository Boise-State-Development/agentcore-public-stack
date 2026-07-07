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
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

from .models import (
    EntryType,
    MemoryEntryRef,
    MemoryIndex,
    MemorySpace,
    Role,
    ShareRole,
    SpaceMember,
)
from .repository import MemorySpaceRepository, OptimisticLockError
from .store import MemorySpaceStore, compute_content_hash, get_memory_space_store
from .templates import DEFAULT_TEMPLATE_ID, get_template, is_valid_template

logger = logging.getLogger(__name__)

_ROLE_RANK: Dict[str, int] = {"viewer": 1, "editor": 2, "owner": 3}

# Bounded read-modify-retry attempts when a shared space's manifest is being
# edited concurrently. Entry writes touch a single slug, so re-reading the
# fresh manifest and re-applying the change is safe; only a sustained race
# exhausts this and surfaces as a conflict.
_MAX_MANIFEST_RETRIES = 5

_T = TypeVar("_T")


class MemorySpaceError(RuntimeError):
    """Base class for memory-space service errors (translated by the API layer)."""


class MemorySpaceNotFoundError(MemorySpaceError):
    """The space does not exist (or the caller may not even know it does)."""


class MemorySpacePermissionError(MemorySpaceError):
    """The caller lacks the required role on the space."""


class MemorySpaceConcurrencyError(MemorySpaceError):
    """A shared space's manifest kept changing under a bounded retry loop.

    Surfaced to the API layer as ``409 Conflict`` — the write is safe to retry
    from a fresh read.
    """


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
    ) -> None:
        self.repository = repository or MemorySpaceRepository()
        self.store = store or get_memory_space_store()

    # ---- permission ----------------------------------------------------

    def resolve_permission(
        self, space_id: str, user_id: str, user_email: Optional[str] = None
    ) -> Tuple[Optional[MemorySpace], Optional[Role]]:
        """Resolve the caller's role on a space.

        Returns ``(space, role)`` where role is ``owner``/``editor``/``viewer``,
        or ``(space, None)`` if the caller has no grant, or ``(None, None)`` if
        the space does not exist. Mirrors ``resolve_assistant_permission``.
        """
        space = self.repository.get_space(space_id)
        if space is None:
            return None, None
        if space.owner_id == user_id:
            return space, "owner"
        if user_email:
            member = self.repository.get_member(space_id, user_email)
            if member is not None:
                return space, member.permission
        return space, None

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
    ) -> MemorySpace:
        """Create a space seeded from a template; returns the persisted space."""
        if not owner_id:
            raise MemorySpaceError("owner_id is required to create a space")
        if not name or not name.strip():
            raise MemorySpaceError("a memory space name is required")
        if not is_valid_template(template):
            raise MemorySpaceError(f"unknown template '{template}'")

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
        )
        self.repository.put_space(space)
        self.repository.put_index(MemoryIndex(space_id=space_id, entries=[], version=0))
        logger.info(
            "memory-spaces: created space=%s owner=%s template=%s",
            space_id,
            owner_id,
            template,
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
        """Delete a space (owner only): all rows + best-effort S3 objects."""
        space, _ = self._require(space_id, user_id, user_email, "owner")
        # Best-effort purge of the byte objects (dedup-aware deletion is a v1
        # concern per the spec's data-governance section; content-addressed
        # objects unreferenced after row deletion are the only residue).
        index = self.repository.get_index(space_id)
        for ref in index.entries:
            self.store.delete(ref.s3_key)
        if space.index_s3_key:
            self.store.delete(space.index_s3_key)
        self.repository.delete_space(space_id)
        logger.info("memory-spaces: deleted space=%s by user=%s", space_id, user_id)

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
        self._require(space_id, actor_id, actor_email, "owner")
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
        self._require(space_id, actor_id, actor_email, "owner")
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
        self._require(space_id, actor_id, actor_email, "owner")
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

    def update_index(
        self,
        space_id: str,
        user_id: str,
        user_email: Optional[str],
        body: str,
    ) -> MemorySpace:
        """Replace the MEMORY.md index text (editor+)."""
        space, _ = self._require(space_id, user_id, user_email, "editor")
        content = body.encode("utf-8")
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
            raise MemorySpaceNotFoundError(
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
        description: str = "",
        indexed: Optional[Dict[str, Any]] = None,
    ) -> MemoryEntryRef:
        """Create or replace an entry (editor+); updates the manifest."""
        self._require(space_id, user_id, user_email, "editor")
        if not slug or not slug.strip():
            raise MemorySpaceError("an entry slug is required")

        content = body.encode("utf-8")
        s3_key = self.store.put(
            space_id=space_id, content=content, content_type="text/markdown"
        )
        ref = MemoryEntryRef(
            slug=slug,
            entry_type=entry_type,
            description=description,
            content_hash=compute_content_hash(content),
            size=len(content),
            s3_key=s3_key,
            updated=_now_iso(),
            updated_by=user_id,
            indexed=indexed or {},
        )

        def apply(index: MemoryIndex) -> List[MemoryEntryRef]:
            old = [e for e in index.entries if e.slug == slug]
            kept = [e for e in index.entries if e.slug != slug]
            kept.append(ref)
            kept.sort(key=lambda e: e.slug)
            index.entries = kept
            return old

        old, final_index = self._mutate_index(space_id, apply)

        # GC any object the replaced entry uniquely referenced.
        for prev in old:
            if prev.s3_key != s3_key and not self._key_in_use(
                space_id, prev.s3_key, index=final_index
            ):
                self.store.delete(prev.s3_key)
        return ref

    def delete_entry(
        self,
        space_id: str,
        user_id: str,
        user_email: Optional[str],
        slug: str,
    ) -> None:
        """Remove an entry from the manifest (editor+) and GC its object."""
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
        for prev in removed:
            if not self._key_in_use(space_id, prev.s3_key, index=final_index):
                self.store.delete(prev.s3_key)

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
        """True if any entry or the space index still references ``s3_key``.

        Objects are content-addressed, so identical content under different
        slugs shares one object — never delete a key another ref still points
        at.
        """
        idx = index if index is not None else self.repository.get_index(space_id)
        if any(e.s3_key == s3_key for e in idx.entries):
            return True
        space = self.repository.get_space(space_id)
        return bool(space and space.index_s3_key == s3_key)

    def _touch(self, space_id: str) -> None:
        space = self.repository.get_space(space_id)
        if space is not None:
            space.updated_at = _now_iso()
            self.repository.put_space(space)
