"""Agent Marketplace Phases 5-6 — the pin read and write (D8, D9).

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

**4. Role-seeded pins are resolved live, per request (D9.1, Phase 6).** The effective list
is *(⋃ role pins for the roles the caller matched) − dismissed(unlocked only) ∪ own pins*.
Nothing is materialized per member, so removing a role pin removes it for everyone who had
not independently pinned the Agent — and a user who *did* pin it has converted the seed to
their own pin, which survives. The role ids come from ``resolve_user_permissions``, whose
``app_roles`` is the list of roles that actually matched (including the ``default``
substitution); the pins themselves are read by their own query, never from
``EffectivePermissions``.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

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
from apis.shared.assistants.listing import is_published
from apis.shared.assistants.publishers import list_publishers
from apis.shared.assistants.role_pins import list_pins_for_roles
from apis.shared.assistants.service import (
    get_assistant_with_access_check,
    resolve_assistant_permission,
)
from apis.shared.auth.models import User
from apis.shared.rbac.service import get_app_role_service

logger = logging.getLogger(__name__)

__all__ = ["PinError", "list_pins", "pin_agent", "unpin_agent"]


class PinError(Exception):
    """A pin operation the caller should see as an HTTP status rather than a 500."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class _Seed:
    """A role pin as it applies to one user, after merging every role that seeds it."""

    locked: bool
    priority: int
    order: int
    created_at: Optional[str]


def _pin_row(
    assistant: Assistant,
    ref: Optional[PinnedAgentRef],
    publishers: dict,
    seed: Optional[_Seed] = None,
) -> PinnedAgentResponse:
    """Project a pinned Agent into the shelf shape.

    The same narrow projection browse uses (D4) — no ``instructions``, no binding refs,
    no owner id — plus ``source``/``locked``, which say *why* the row is on the shelf.

    ``source`` is ``"user"`` whenever the caller has their own pin, even when a role also
    seeds the Agent: that is the D9.1 escape hatch, and it is what survives the role pin
    being removed. ``locked`` follows the *seed* regardless of source — a role that locks
    an Agent has said its members keep it, and a user who separately pinned it has not
    contradicted that.
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
        source="user" if ref is not None else "role",
        locked=bool(seed and seed.locked),
        pinned_at=(ref.pinned_at if ref else (seed.created_at if seed else None)),
    )


async def _role_seeds(user: User) -> Dict[str, _Seed]:
    """Every Agent seeded to this user by a role they hold (D9.1, D9.2).

    ``resolve_user_permissions`` is used *only* for its ``app_roles`` — the roles that
    actually matched, including the ``default`` substitution when nothing did. The pins
    are then read by their own query. Nothing here reads or writes ``EffectivePermissions``:
    a pin is not a permission, and folding one into that structure would put it on the
    model call path.

    **Pins from multiple roles merge, with no precedence rules (D9.2).** The union is what
    a user holding both ``staff`` and ``faculty`` gets; the only per-Agent decisions are
    that a lock from *any* role wins (the strictest claim is the honest one) and that the
    ordering hints come from the highest-priority role that seeds it.
    """
    service = get_app_role_service()
    try:
        permissions = await service.resolve_user_permissions(user)
    except Exception:
        # A shelf that renders the user's own pins beats a shelf that fails to render.
        logger.warning("Failed to resolve roles for default pins", exc_info=True)
        return {}

    role_ids = list(permissions.app_roles or [])
    if not role_ids:
        return {}

    by_role = await list_pins_for_roles(role_ids)

    seeds: Dict[str, _Seed] = {}
    for role_id, pins in by_role.items():
        if not pins:
            continue
        role = await service.get_role(role_id)
        priority = role.priority if role else 0
        for pin in pins:
            current = seeds.get(pin.agent_id)
            if current is None:
                seeds[pin.agent_id] = _Seed(
                    locked=pin.locked,
                    priority=priority,
                    order=pin.order,
                    created_at=pin.created_at,
                )
                continue
            wins = (priority, -pin.order) > (current.priority, -current.order)
            seeds[pin.agent_id] = _Seed(
                locked=current.locked or pin.locked,
                priority=priority if wins else current.priority,
                order=pin.order if wins else current.order,
                created_at=(pin.created_at if wins else current.created_at),
            )

    return seeds


def _shelf_key(
    ref: Optional[PinnedAgentRef], seed: Optional[_Seed], name: str
) -> Tuple[int, int, int, int, str]:
    """``(locked desc, role priority desc, order asc, name asc)`` from the spec's Data model.

    The extra term is where a pin with no role sits: a user's own pins have no role
    priority to sort by, so they follow the seeded ones rather than being interleaved by a
    priority they do not have. Within each group the stored ``order`` holds.
    """
    if seed is not None:
        return (0 if seed.locked else 1, 0, -seed.priority, seed.order, name.lower())
    return (1, 1, 0, ref.order if ref else 0, name.lower())


async def list_pins(user: User) -> List[PinnedAgentResponse]:
    """The user's effective pin list, in shelf order (D9).

    ``(⋃ role pins) − dismissed(unlocked only) ∪ own pins``. The tombstones are the point:
    seeds resolve live, so without them a dismissed seed would re-apply on the very next
    request and the user could never remove it. A *locked* seed ignores the tombstone by
    design (D9.4) — that is the whole meaning of the flag.
    """
    state = await get_pin_state(user.user_id)
    own: Dict[str, PinnedAgentRef] = {ref.agent_id: ref for ref in state.pinned}
    dismissed = set(state.dismissed)
    seeds = await _role_seeds(user)

    candidates: List[Tuple[str, Optional[PinnedAgentRef], Optional[_Seed]]] = []
    for agent_id, seed in seeds.items():
        ref = own.get(agent_id)
        if ref is None and agent_id in dismissed and not seed.locked:
            continue
        candidates.append((agent_id, ref, seed))
    for agent_id, ref in own.items():
        if agent_id not in seeds:
            candidates.append((agent_id, ref, None))

    if not candidates:
        return []

    publishers = {p.id: p for p in await list_publishers()}

    rows: List[Tuple[Tuple[int, int, int, int, str], PinnedAgentResponse]] = []
    for agent_id, ref, seed in candidates:
        try:
            assistant, permission = await get_assistant_with_access_check(
                agent_id, user.user_id, user.email
            )
        except Exception:
            logger.warning(f"Failed to resolve pinned agent {agent_id}", exc_info=True)
            continue

        # Deleted since it was pinned, or no longer reachable by this user. Dropped from
        # the response; the stored pin is left alone, because a read is not the place to
        # garbage-collect writes and both conditions are reversible. A role seed the
        # caller cannot reach drops here too — the pin never becomes a grant.
        if assistant is None or permission is None:
            continue

        row = _pin_row(assistant, ref, publishers, seed)
        rows.append((_shelf_key(ref, seed, assistant.name), row))

    rows.sort(key=lambda entry: entry[0])
    return [row for _key, row in rows]


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
        # Not-found and access-denied normally collapse: telling a stranger that an id
        # exists but is not theirs is a disclosure the store has no reason to make.
        #
        # The exception is an Agent the store has *already advertised*. Its existence is
        # not a secret — the caller is here because a published tile offered them an Add
        # button — so collapsing to "not found" withholds nothing and leaves them with an
        # error that contradicts the page they are looking at. Publication now requires
        # PUBLIC, so this should only be reachable for a listing narrowed after approval;
        # it is the failure that has no submit-time gate, which is exactly why it must
        # explain itself. The extra read costs nothing on the success path.
        #
        # Best-effort: this lookup exists only to word the failure better, so it must
        # never make the failure worse. A raise here would turn a well-defined 404 into a
        # 500 on a path that already knows its answer.
        try:
            existing, _ = await resolve_assistant_permission(agent_id, user.user_id, user.email)
        except Exception:
            logger.warning(
                f"Could not classify pin denial for {agent_id}; falling back to 404",
                exc_info=True,
            )
            existing = None
        if existing is not None and existing.listing and is_published(existing.listing.state):
            logger.warning(
                f"Pin denied on published agent {agent_id} for {user.user_id}: "
                f"visibility is {existing.visibility}"
            )
            raise PinError(
                403,
                "This agent is listed in the store but its owner has restricted who can "
                "open it, so it can't be added right now.",
            )
        raise PinError(404, f"Agent not found: {agent_id}")

    try:
        state = await add_pin(user.user_id, agent_id)
    except PinLimitError as e:
        raise PinError(409, str(e)) from e

    ref = next((r for r in state.pinned if r.agent_id == agent_id), None)
    publishers = {p.id: p for p in await list_publishers()}
    # The seed is resolved for the response row too: the SPA splices this row straight
    # into its list, so a row that said ``locked: false`` about an Agent the caller's role
    # has locked would offer a remove control until the next full read took it away.
    seed = (await _role_seeds(user)).get(agent_id)
    return _pin_row(assistant, ref, publishers, seed)


async def unpin_agent(user: User, agent_id: str) -> None:
    """Unpin an Agent and record the dismissal (D9.3).

    No existence check: unpinning a deleted Agent has to work, or a user whose pinned
    Agent was removed is stuck with a row they cannot clear.

    **A locked role seed no-ops** (D9.4). The SPA hides the control, but the endpoint is
    reachable directly, and writing the tombstone anyway would be worse than refusing: the
    row would come back on the next read (a locked seed ignores tombstones) while the
    stored dismissal quietly waited to take effect the day an admin unlocked the pin.
    """
    seed = (await _role_seeds(user)).get(agent_id)
    if seed is not None and seed.locked:
        logger.info(f"📌 {user.user_id} tried to unpin locked role pin {agent_id}; ignored")
        return
    await remove_pin(user.user_id, agent_id)
