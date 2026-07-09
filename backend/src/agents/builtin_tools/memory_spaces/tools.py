"""Context-bound tools for an Agent's bound Memory Space (Agent Designer Phase 3).

Each factory closes over the *binding's* space id and the *invoking* user's identity, so
the returned tool can physically only address that one space as that one user — it cannot
be re-pointed at another space. ``MemorySpaceService`` re-checks the caller's grant
(``viewer+`` for reads, ``editor+`` for writes) on every call, so a permission revoked
mid-session surfaces as an error tool-result on the next call rather than leaking access.
Same closure-identity + ``asyncio.to_thread`` pattern as the artifact/spreadsheet tools
(the codebase has no tool-execution contextvar; ``MemorySpaceService`` is sync boto3).

These are injected via the invocation path's ``extra_tools`` seam only when an Agent has a
resolved ``memory_space`` binding — agents with ``extra_tools`` are never cached, so a tool
closed over user A's identity can never be served to user B.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from strands import tool

from apis.shared.memory.service import (
    MemorySpaceError,
    MemorySpaceNotFoundError,
    MemorySpacePermissionError,
    MemorySpaceService,
)

logger = logging.getLogger(__name__)

# The space's human-readable index (MEMORY.md) is not a manifest entry — it's a
# standalone S3 object addressed by ``space.index_s3_key`` and auto-injected into
# the agent's context each session via hydration (``always_load`` starts with
# "MEMORY.md"). It never appears in ``memory_list``. To let the agent keep that
# index in sync with the entries it writes, ``memory_read``/``memory_write`` treat
# this reserved slug specially, routing to ``read_index``/``update_index`` instead
# of the entry manifest. The slug is reserved: an agent cannot create an ordinary
# entry named "MEMORY.md".
_INDEX_SLUG = "MEMORY.md"


def _is_index_slug(slug: str) -> bool:
    return slug.strip().lower() == _INDEX_SLUG.lower()


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"text": f"❌ {text}"}], "status": "error"}


def make_memory_list_tool(space_id: str, space_name: str, user_id: str, user_email: Optional[str]):
    @tool
    async def memory_list(entry_type: Optional[str] = None) -> dict[str, Any]:
        """List the entries in your bound memory space (manifest only — no content).

        Use this to see what you remember before deciding whether to `memory_read` a
        specific entry. Returns each entry's slug, type, description, and last-updated
        time. Optionally filter by `entry_type` ("entity", "episodic", or "fact").

        The `MEMORY.md` index is not an entry and never appears here — it's injected
        into your context each session and is read/written via `memory_read("MEMORY.md")`
        / `memory_write("MEMORY.md", ...)`.

        Args:
            entry_type: Optional filter — one of "entity", "episodic", "fact".
        """
        try:
            entries = await asyncio.to_thread(
                MemorySpaceService().list_entries,
                space_id, user_id, user_email, entry_type=entry_type,
            )
        except MemorySpacePermissionError as exc:
            return _error(f"You no longer have access to memory space '{space_name}': {exc}")
        except MemorySpaceError as exc:
            return _error(f"Could not list memory: {exc}")

        summary = [
            {"slug": e.slug, "type": e.entry_type, "description": e.description, "updated": e.updated}
            for e in entries
        ]
        return {"content": [{"json": {"entries": summary}}], "status": "success"}

    return memory_list


def make_memory_read_tool(space_id: str, space_name: str, user_id: str, user_email: Optional[str]):
    @tool
    async def memory_read(slug: str) -> dict[str, Any]:
        """Read the full content of one entry in your bound memory space.

        Pass a `slug` from `memory_list` (or referenced in your injected memory index).
        The special slug `MEMORY.md` reads the space's human-readable index (the same
        text injected into your context each session) — use it to see the current index
        before updating it with `memory_write`.

        Args:
            slug: The entry's stable id within the space (e.g. "jane-doe"), or the
                reserved slug "MEMORY.md" for the space index.
        """
        try:
            if _is_index_slug(slug):
                body = await asyncio.to_thread(
                    MemorySpaceService().read_index, space_id, user_id, user_email
                )
            else:
                body = await asyncio.to_thread(
                    MemorySpaceService().read_entry, space_id, user_id, user_email, slug
                )
        except MemorySpaceNotFoundError:
            return _error(f"No memory entry '{slug}' exists in '{space_name}'.")
        except MemorySpacePermissionError as exc:
            return _error(f"You no longer have access to memory space '{space_name}': {exc}")
        except MemorySpaceError as exc:
            return _error(f"Could not read memory entry '{slug}': {exc}")
        return {"content": [{"text": body}], "status": "success"}

    return memory_read


def make_memory_write_tool(space_id: str, space_name: str, user_id: str, user_email: Optional[str]):
    @tool
    async def memory_write(
        slug: str,
        body: str,
        entry_type: str = "fact",
        description: str = "",
    ) -> dict[str, Any]:
        """Create or replace an entry in your bound memory space (persists across sessions).

        Use this to remember durable facts, people/entities, or episodic notes the user
        will want recalled later. Writing an existing `slug` replaces that entry. Only
        available when the agent's memory binding grants write access.

        The special slug `MEMORY.md` replaces the space's human-readable index instead of
        creating an entry — keep it in sync as you add entries (e.g. a one-line pointer or
        `[[slug]]` wikilink per entry). Read the current index first with
        `memory_read("MEMORY.md")`; `entry_type` and `description` are ignored for it.

        Args:
            slug: Stable id for the entry (e.g. "jane-doe", "daily-2026-07-07"), or the
                reserved slug "MEMORY.md" to replace the space index.
            body: The entry's (or index's) markdown content.
            entry_type: "entity", "episodic", or "fact" (default "fact").
            description: Short one-line summary shown in listings.
        """
        try:
            if _is_index_slug(slug):
                await asyncio.to_thread(
                    MemorySpaceService().update_index,
                    space_id, user_id, user_email, body,
                )
                return {
                    "content": [{"text": f'Updated the MEMORY.md index of "{space_name}".'}],
                    "status": "success",
                }
            ref = await asyncio.to_thread(
                lambda: MemorySpaceService().write_entry(
                    space_id, user_id, user_email, slug, body,
                    entry_type=entry_type, description=description,
                )
            )
        except MemorySpacePermissionError as exc:
            return _error(f"You don't have write access to memory space '{space_name}': {exc}")
        except MemorySpaceError as exc:
            return _error(f"Could not write memory entry '{slug}': {exc}")
        return {
            "content": [{"text": f'Saved memory entry "{ref.slug}" ({ref.entry_type}) to "{space_name}".'}],
            "status": "success",
        }

    return memory_write
