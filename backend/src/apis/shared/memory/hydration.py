"""Agent Designer Phase 3 — Memory-Space content hydration for prompt injection.

Resolves a ``memory_space`` binding's ``config.alwaysLoad`` list into concrete text
fragments to inject into an Agent's system prompt at invocation. Lives in ``apis.shared``
so both the Harness (inference-api) and any future app-api preview/"context breakdown"
surface can reuse it.

``alwaysLoad`` addressing scheme (see ``templates.py`` / the memory spec):
- ``"MEMORY.md"``            → the space index text (``read_index``).
- ``"latest:<type>/<prefix>"`` → the most-recently-updated manifest entry whose
  ``entry_type`` matches ``<type>`` and whose slug starts with ``<prefix>`` (e.g.
  ``latest:episodic/daily``). If ``<type>`` isn't a valid ``EntryType`` the whole
  remainder is treated as a slug prefix with no type filter.
- any other string          → an exact entry slug (``read_entry``).

A missing entry is skipped (an empty space, or an entry deleted since the binding was
authored, must never fail the turn). Injection is budget-capped: over-budget fragments are
truncated with a marker so the model knows to fetch the rest via a ``memory_read`` tool.
All reads run through ``MemorySpaceService``, which re-checks the caller's ``viewer+`` grant
internally — so hydration cannot leak a space the invoker can't read.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

from apis.shared.memory.service import MemorySpaceNotFoundError, MemorySpaceService

_VALID_ENTRY_TYPES = {"entity", "episodic", "fact"}
DEFAULT_ALWAYS_LOAD = ["MEMORY.md"]

# Total injected memory budget (bytes). ~24 KB ≈ 6k tokens — headroom over the
# documented steady state (~200 entries ≈ 4k tokens). Override per environment.
_DEFAULT_MAX_TOTAL_BYTES = 24_000
_TRUNCATION_MARKER = "\n…[truncated — use memory_read to fetch the full entry]"


@dataclass
class LoadedFragment:
    """One resolved piece of memory to inject: a human label + its text."""

    label: str
    text: str


def _max_total_bytes() -> int:
    raw = os.environ.get("MEMORY_INJECTION_MAX_BYTES")
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return _DEFAULT_MAX_TOTAL_BYTES


def _resolve_latest(
    service: MemorySpaceService, space_id: str, user_id: str, user_email: Optional[str], rest: str
) -> Optional[tuple]:
    """Resolve a ``latest:<type>/<prefix>`` spec → (slug, text) or None."""
    if "/" in rest:
        type_part, prefix = rest.split("/", 1)
    else:
        type_part, prefix = rest, ""
    entry_type = type_part if type_part in _VALID_ENTRY_TYPES else None
    # If the first segment isn't a valid type, treat the whole remainder as a slug prefix.
    if entry_type is None:
        prefix = rest

    entries = service.list_entries(space_id, user_id, user_email, entry_type=entry_type)
    if prefix:
        entries = [e for e in entries if e.slug.startswith(prefix)]
    if not entries:
        return None
    latest = max(entries, key=lambda e: e.updated)
    return latest.slug, service.read_entry(space_id, user_id, user_email, latest.slug)


def resolve_always_load(
    service: MemorySpaceService,
    space_id: str,
    user_id: str,
    user_email: Optional[str],
    always_load: Optional[List[str]],
    *,
    max_total_bytes: Optional[int] = None,
) -> List[LoadedFragment]:
    """Resolve ``always_load`` specs into injectable fragments, within a byte budget.

    Synchronous (``MemorySpaceService`` is sync boto3) — callers on the event loop wrap
    this in ``asyncio.to_thread``. Never raises for a missing entry; a genuinely broken
    read (permission revoked mid-turn, store error) propagates.
    """
    specs = always_load if always_load else DEFAULT_ALWAYS_LOAD
    budget = _max_total_bytes() if max_total_bytes is None else max_total_bytes

    fragments: List[LoadedFragment] = []
    used = 0
    for spec in specs:
        if used >= budget:
            break
        try:
            if spec == "MEMORY.md":
                text, label = service.read_index(space_id, user_id, user_email), "MEMORY.md"
            elif spec.startswith("latest:"):
                resolved = _resolve_latest(service, space_id, user_id, user_email, spec[len("latest:"):])
                if resolved is None:
                    continue
                slug, text = resolved
                label = f"{spec} → {slug}"
            else:
                text, label = service.read_entry(space_id, user_id, user_email, spec), spec
        except MemorySpaceNotFoundError:
            # Entry/index deleted since the binding was authored — skip, don't fail.
            continue

        if not text:
            continue

        encoded = text.encode("utf-8")
        remaining = budget - used
        if len(encoded) > remaining:
            text = encoded[:remaining].decode("utf-8", errors="ignore") + _TRUNCATION_MARKER
            used = budget
        else:
            used += len(encoded)
        fragments.append(LoadedFragment(label=label, text=text))

    return fragments


def render_memory_block(space_name: str, fragments: List[LoadedFragment]) -> str:
    """Render resolved fragments into a delimited system-prompt block.

    Empty when there are no fragments (a fresh space injects nothing).
    """
    if not fragments:
        return ""
    parts = [
        f'## Bound Memory — "{space_name}"',
        "This is your persistent memory for this agent. Fetch more with `memory_read` / "
        "list with `memory_list`.",
    ]
    for frag in fragments:
        parts.append(f"### {frag.label}\n{frag.text}")
    return "\n\n".join(parts)
