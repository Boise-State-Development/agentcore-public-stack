"""Scope-addressed Memory-Space tools for a project's harness (Shared Projects 2.4b).

A project's harness Agent reaches two spaces by scope instead of by binding:
``project`` is the project's shared space and ``mine`` is the invoking member's
``personal_in_project`` space. The tools close over one :class:`ProjectMemoryScopes`
(the project id, the two space ids and the member), so they can only address that
project's spaces as that member. ``MemorySpaceService`` re-checks the member's role
on every call, from the project (``resolve_permission`` is project-aware since 2.4a),
so a member removed or demoted mid-session gets an error result on the next call.

Ordinary Agents keep the binding-addressed ``memory_list/read/write`` in
:mod:`.tools`, byte for byte: a harness never gets those, and no ordinary Agent gets
these. The agent cache key carries a digest of the ids closed over here
(``memory_binding_digest`` on the project shape).

A member's own space is created by their first ``memory_save(scope="mine")``, not
when the turn starts (2.4a): three writes before the first token of every member's
first turn would cost more than they buy.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal, Optional

from strands import tool

from apis.shared.auth.models import User
from apis.shared.memory.service import (
    MemoryEntryNotFoundError,
    MemorySpaceError,
    MemorySpaceNotFoundError,
    MemorySpacePermissionError,
    MemorySpaceService,
    MemoryValidationError,
)
from apis.shared.projects.service import ProjectConflictError, ProjectError, ProjectNotFoundError

logger = logging.getLogger(__name__)

Scope = Literal["project", "mine"]
SCOPES = ("project", "mine")
_LABELS = {"project": "project memory", "mine": "your memory in this project"}
_INDEX_SLUG = "MEMORY.md"
_ARCHIVED = "This project is archived, so its memory is read-only."
_NOT_A_MEMBER = "You are no longer a member of this project, so its memory is unavailable."


def _is_index_slug(slug: str) -> bool:
    return slug.strip().lower() == _INDEX_SLUG.lower()


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"text": f"❌ {text}"}], "status": "error"}


def _bad_scope(scope: str) -> Optional[dict[str, Any]]:
    if scope in SCOPES:
        return None
    return _error(f'Unknown scope "{scope}". Use "project" or "mine".')


@dataclass
class ProjectMemoryScopes:
    """What the harness tools close over: one project, its two spaces, one member.

    ``personal_space_id`` is learned lazily. A member without a space yet gets
    one on their first save; the pointer never changes after that (it goes
    only when the project is purged), so a cached agent may keep the id.
    """

    project_id: str
    shared_space_id: Optional[str]
    personal_space_id: Optional[str]
    user: User

    @classmethod
    def for_member(
        cls, project_id: str, shared_space_id: Optional[str], personal_space_id: Optional[str], user: User
    ) -> "ProjectMemoryScopes":
        # A copy without the bearer token: the tools may outlive this request
        # in the agent cache and never need it.
        member = User(email=user.email, user_id=user.user_id, name=user.name, roles=list(user.roles or []))
        return cls(project_id, shared_space_id, personal_space_id, member)

    def space_id(self, scope: str) -> Optional[str]:
        """The space behind ``scope``, or None if it does not exist (yet). Sync."""
        if scope == "project":
            return self.shared_space_id
        if self.personal_space_id is None and self.user.user_id:
            from apis.shared.projects.repository import ProjectRepository

            self.personal_space_id = ProjectRepository().get_personal_space_id(self.project_id, self.user.user_id)
        return self.personal_space_id

    def ensure_personal_space(self) -> str:
        """The member's space, created through the project on first use. Sync.

        ``ProjectService`` settles a race between two first saves: the pointer
        write is conditional, and the loser deletes the space it made.
        """
        if self.space_id("mine"):
            return self.personal_space_id  # type: ignore[return-value]
        from apis.shared.projects.service import ProjectService

        self.personal_space_id = ProjectService().get_or_create_personal_space(self.project_id, self.user)
        return self.personal_space_id

    def project_is_archived(self) -> bool:
        from apis.shared.projects.repository import ProjectRepository

        project = ProjectRepository().get_project(self.project_id)
        return project is not None and project.status == "archived"


def _missing(scope: str) -> str:
    if scope == "project":
        return "This project has no shared memory yet."
    return "You have not saved anything to your own memory in this project yet."


def make_project_memory_list_tool(scopes: ProjectMemoryScopes):
    @tool
    async def memory_list(scope: Scope) -> dict[str, Any]:
        """List the files in project memory, without their content.

        `scope` is "project" (shared with every member of this project) or "mine"
        (your own notes in this project; only you can see them). Returns each file's
        name, description, last update and approximate size in tokens. The MEMORY.md
        index is not listed; read it with `memory_read`.

        Args:
            scope: "project" or "mine".
        """
        if (bad := _bad_scope(scope)) is not None:
            return bad
        try:
            space_id = await asyncio.to_thread(scopes.space_id, scope)
            if space_id is None:
                return {"content": [{"json": {"files": [], "note": _missing(scope)}}], "status": "success"}
            entries = await asyncio.to_thread(
                MemorySpaceService().list_entries, space_id, scopes.user.user_id, scopes.user.email
            )
        except (MemorySpacePermissionError, MemorySpaceNotFoundError):
            return _error(_NOT_A_MEMBER)
        except MemorySpaceError as exc:
            return _error(f"Could not list {_LABELS[scope]}: {exc}")
        return {"content": [{"json": {"files": [_summary(e) for e in entries]}}], "status": "success"}

    return memory_list


def _summary(entry: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"slug": entry.slug, "description": entry.description, "updated": entry.updated}
    if getattr(entry, "tokens", None) is not None:
        row["tokens"] = entry.tokens
    if getattr(entry, "aliases", None):
        row["aliases"] = list(entry.aliases)
    return row


def make_project_memory_read_tool(scopes: ProjectMemoryScopes):
    @tool
    async def memory_read(scope: Scope, slug: str) -> dict[str, Any]:
        """Read one memory file in full.

        Items end with `<!-- e:… -->` anchors. Keep each anchor on its item when you
        save an edited file.

        Args:
            scope: "project" or "mine".
            slug: A file name from `memory_list` or a `[[link]]`, or "MEMORY.md" for the index.
        """
        if (bad := _bad_scope(scope)) is not None:
            return bad
        user = scopes.user
        try:
            space_id = await asyncio.to_thread(scopes.space_id, scope)
            if space_id is None:
                return _error(_missing(scope))
            service = MemorySpaceService()
            if _is_index_slug(slug):
                body = await asyncio.to_thread(service.read_index, space_id, user.user_id, user.email)
            else:
                body = await asyncio.to_thread(service.read_entry, space_id, user.user_id, user.email, slug)
        except MemoryEntryNotFoundError:
            return _error(f"There is no file '{slug}' in {_LABELS[scope]}.")
        except (MemorySpacePermissionError, MemorySpaceNotFoundError):
            return _error(_NOT_A_MEMBER)
        except MemorySpaceError as exc:
            return _error(f"Could not read '{slug}': {exc}")
        return {"content": [{"text": body}], "status": "success"}

    return memory_read


_QUERY_KEYS = ("text", "updated_after")


def make_project_memory_query_tool(scopes: ProjectMemoryScopes):
    @tool
    async def memory_query(scope: Scope, where: dict[str, str]) -> dict[str, Any]:
        """Find memory files by name, description or date, without reading them.

        Args:
            scope: "project" or "mine".
            where: Filters, each optional: "text" matches a file's name, description
                or aliases (case-insensitive); "updated_after" is an ISO date such as
                "2026-09-01".
        """
        if (bad := _bad_scope(scope)) is not None:
            return bad
        unknown = sorted(set(where or {}) - set(_QUERY_KEYS))
        if unknown:
            return _error(f"Unknown filter {', '.join(unknown)}. Use \"text\" and/or \"updated_after\".")
        needle = str((where or {}).get("text") or "").strip().casefold()
        after = str((where or {}).get("updated_after") or "").strip()
        try:
            space_id = await asyncio.to_thread(scopes.space_id, scope)
            if space_id is None:
                return {"content": [{"json": {"files": [], "note": _missing(scope)}}], "status": "success"}
            entries = await asyncio.to_thread(
                MemorySpaceService().list_entries, space_id, scopes.user.user_id, scopes.user.email
            )
        except (MemorySpacePermissionError, MemorySpaceNotFoundError):
            return _error(_NOT_A_MEMBER)
        except MemorySpaceError as exc:
            return _error(f"Could not search {_LABELS[scope]}: {exc}")

        def matches(entry: Any) -> bool:
            if after and (entry.updated or "") <= after:
                return False
            if needle:
                haystack = [entry.slug, entry.description or "", *(getattr(entry, "aliases", None) or [])]
                return any(needle in field.casefold() for field in haystack)
            return True

        files = [_summary(e) for e in entries if matches(e)]
        return {"content": [{"json": {"files": files}}], "status": "success"}

    return memory_query


def make_project_memory_save_tool(scopes: ProjectMemoryScopes):
    @tool
    async def memory_save(scope: Scope, slug: str, text: str, description: str = "") -> dict[str, Any]:
        """Save a memory file, creating it or replacing it whole. It persists across conversations.

        A file is a list with one fact per "- " line, such as "- The pilot starts
        March 3."; prose and headings are rejected. To change a file, `memory_read` it
        first and send back the whole list: keep each item's `<!-- e:… -->` anchor, add
        new items without one, and leave out items to remove them. Link other files
        with `[[name]]`.

        "mine" is always yours to write. "project" is shared with every member and needs
        the editor role; a viewer can save to "mine" instead or ask an editor. The slug
        "MEMORY.md" replaces that scope's index, which appears in every conversation:
        keep it to one short line per file, such as "- [[vendor]] — vendor decisions".

        Args:
            scope: "project" or "mine".
            slug: The file name: lowercase words joined by "-", optionally grouped
                with "/" (e.g. "decisions/vendor"), or "MEMORY.md" for the index.
            text: The file's items, one "- " line each.
            description: Optional one-line summary shown in listings.
        """
        if (bad := _bad_scope(scope)) is not None:
            return bad
        user = scopes.user
        label = _LABELS[scope]
        try:
            if scope == "mine":
                space_id = await asyncio.to_thread(scopes.ensure_personal_space)
            else:
                space_id = await asyncio.to_thread(scopes.space_id, scope)
                if space_id is None:
                    return _error(f'{_missing(scope)} Save it to "mine" instead.')
            service = MemorySpaceService()
            if _is_index_slug(slug):
                await asyncio.to_thread(service.update_index, space_id, user.user_id, user.email, text)
                return {"content": [{"text": f"Updated the MEMORY.md index of {label}."}], "status": "success"}
            result = await asyncio.to_thread(
                lambda: service.save_entry(
                    space_id, user.user_id, user.email, slug, text,
                    description=description or None, reason="save",
                )
            )
        except MemoryValidationError as exc:
            return _error(f"Not saved: {exc}")
        except MemorySpacePermissionError:
            return _error(await _refusal(scopes, scope))
        except MemorySpaceNotFoundError:
            return _error(_NOT_A_MEMBER)
        except MemorySpaceError as exc:
            return _error(f"Could not save '{slug}': {exc}")
        except ProjectError as exc:  # the first save to "mine" creates the space
            return _error(_project_error(exc))
        return {"content": [{"text": _saved(result, label)}], "status": "success"}

    return memory_save


async def _refusal(scopes: ProjectMemoryScopes, scope: str) -> str:
    """Why a save was refused. Archived first: an archived project makes everyone a viewer."""
    try:
        if await asyncio.to_thread(scopes.project_is_archived):
            return _ARCHIVED
    except Exception:
        logger.warning("Could not read project %s after a refused memory save", scopes.project_id, exc_info=True)
    if scope == "project":
        return (
            'Only project editors can save to project memory. Save it to "mine" instead, '
            "or ask an editor to add it."
        )
    return _NOT_A_MEMBER


def _project_error(exc: ProjectError) -> str:
    if isinstance(exc, ProjectConflictError):
        return _ARCHIVED
    if isinstance(exc, ProjectNotFoundError):
        return _NOT_A_MEMBER
    return f"Could not set up your memory in this project: {exc}"


def _saved(result: Any, label: str) -> str:
    ref = result.ref
    text = f'Saved "{ref.slug}" to {label} (version {ref.version}'
    text += f", about {ref.tokens:,} tokens)." if ref.tokens is not None else ")."
    if ref.version == 1:
        text += f' To show it in every conversation, add a line like "- [[{ref.slug}]] — what it holds" to MEMORY.md.'
    if result.warnings:
        text += " Notes: " + " ".join(result.warnings)
    return text


def make_project_memory_tools(scopes: ProjectMemoryScopes) -> list:
    """The harness's four memory tools, in a fixed order (their specs are prompt-cached)."""
    return [
        make_project_memory_list_tool(scopes),
        make_project_memory_read_tool(scopes),
        make_project_memory_query_tool(scopes),
        make_project_memory_save_tool(scopes),
    ]
