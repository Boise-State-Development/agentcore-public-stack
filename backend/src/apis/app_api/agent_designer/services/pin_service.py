"""Agent Marketplace Phase 5 — the pin read and write (D8, D9 user side).

"Add to my agents" stores a pointer (D8). This module turns that pointer list back into
something renderable, which is the whole of the work: the stored state is ids, and a shelf
needs icon, name, tagline and publisher.

Three rules are load-bearing:

**1. The pin list is access-checked on every read.** A pin is a bookmark, not a grant.
``get_assistant_with_access_check`` is the same gate the detail page uses, so an Agent
that goes ``PUBLIC`` → ``PRIVATE`` disappears from every stranger's Pinned tab at once,
and nothing here can hand a user a row they could not have reached by navigating. Denied
rows are dropped from the *response*, never from the stored pin: visibility is reversible,
and deleting someone's pin because an author toggled a setting for an afternoon would be a
silent, unrecoverable edit to their shelf.

**2. A pin survives a takedown.** D2 is explicit that delisting is not revocation —
existing pins keep working and conversations underway keep running. So the projection here
tolerates an Agent with no ``listing`` block at all, unlike the browse read, whose whole
safety property is that it can only see published rows.

**3. One read per pin, through the access check, rather than a batch read plus a second
access path.** ``get_assistant_with_access_check`` already fetches the record, so batching
the fetch would mean either re-reading each Agent anyway or restating the visibility rules
here — a second copy of an access decision, which is how a resource ends up *listed* by one
path and *denied* by another (the ``can_access_model`` divergence in CLAUDE.md's RBAC
rule). A pin list is bounded by ``MAX_PINS`` and is normally a handful of ids; a duplicated
access rule is unbounded in cost.
"""

import logging
from typing import List, Optional

from apis.shared.assistants.icons import icon_url
from apis.shared.assistants.models import (
    Assistant,
    ListingPublisher,
    PinnedAgentRef,
    PinnedAgentResponse,
    PublisherProfile,
)
from apis.shared.assistants.pins import (
    PinLimitError,
    add_pin,
    get_pin_state,
    remove_pin,
)
from apis.shared.assistants.publishers import list_publishers
from apis.shared.assistants.service import get_assistant_with_access_check
from apis.shared.auth.models import User

logger = logging.getLogger(__name__)

__all__ = ["PinError", "list_pins", "pin_agent", "unpin_agent"]


class PinError(Exception):
    """A pin operation the caller should see as an HTTP status rather than a 500."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _pin_row(
    assistant: Assistant, ref: Optional[PinnedAgentRef], publishers: dict
) -> PinnedAgentResponse:
    """Project a pinned Agent into the shelf shape.

    The same narrow projection browse uses (D4) — no ``instructions``, no binding refs,
    no owner id — plus ``source``/``locked``, which are constant in Phase 5 and become
    meaningful when role-seeded pins land (D9).
    """
    listing = assistant.listing
    profile: Optional[PublisherProfile] = (
        publishers.get(listing.publisher_id) if listing else None
    )
    return PinnedAgentResponse(
        agent_id=assistant.assistant_id,
        name=assistant.name,
        tagline=assistant.tagline,
        emoji=assistant.emoji,
        icon_url=icon_url(assistant.assistant_id, assistant.icon_key),
        publisher=(
            ListingPublisher(label=profile.label, kind=profile.kind, verified=profile.verified)
            if profile
            else None
        ),
        category=(listing.category if listing else ""),
        source="user",
        locked=False,
        pinned_at=ref.pinned_at if ref else None,
    )


async def list_pins(user: User) -> List[PinnedAgentResponse]:
    """The user's effective pin list, in shelf order (D9).

    Phase 5 resolves the user's own pins only. Phase 6 unions role-seeded pins in here —
    ``(⋃ role pins) − dismissed ∪ own pins`` — which is why the dismissal tombstones are
    already being written and why ``source``/``locked`` are already on the row.
    """
    state = await get_pin_state(user.user_id)
    refs = sorted(state.pinned, key=lambda ref: ref.order)
    if not refs:
        return []

    publishers = {p.id: p for p in await list_publishers()}

    rows: List[PinnedAgentResponse] = []
    for ref in refs:
        try:
            assistant, permission = await get_assistant_with_access_check(
                ref.agent_id, user.user_id, user.email
            )
        except Exception:
            logger.warning(f"Failed to resolve pinned agent {ref.agent_id}", exc_info=True)
            continue

        # Deleted since it was pinned, or no longer reachable by this user. Dropped from
        # the response; the stored pin is left alone, because a read is not the place to
        # garbage-collect writes and both conditions are reversible.
        if assistant is None or permission is None:
            continue

        rows.append(_pin_row(assistant, ref, publishers))

    return rows


async def pin_agent(user: User, agent_id: str) -> PinnedAgentResponse:
    """Pin an Agent for this user (D8).

    Gated on the viewer being able to *reach* the Agent, not on it being published: an
    author pinning their own draft, or a colleague pinning something shared with them, is
    the same "keep this to hand" gesture the store's Add button performs, and the `@`
    menu (D11) is scoped to exactly "your own and pinned Agents". Publication decides what
    the store *offers*, not what a user may bookmark.
    """
    assistant, permission = await get_assistant_with_access_check(
        agent_id, user.user_id, user.email
    )
    if assistant is None or permission is None:
        # Not-found and access-denied deliberately collapse: telling a stranger that an id
        # exists but is not theirs is a disclosure the store has no reason to make.
        raise PinError(404, f"Agent not found: {agent_id}")

    try:
        state = await add_pin(user.user_id, agent_id)
    except PinLimitError as e:
        raise PinError(409, str(e)) from e

    ref = next((r for r in state.pinned if r.agent_id == agent_id), None)
    publishers = {p.id: p for p in await list_publishers()}
    return _pin_row(assistant, ref, publishers)


async def unpin_agent(user: User, agent_id: str) -> None:
    """Unpin an Agent and record the dismissal (D9.3).

    No existence check: unpinning a deleted Agent has to work, or a user whose pinned
    Agent was removed is stuck with a row they cannot clear.
    """
    await remove_pin(user.user_id, agent_id)
