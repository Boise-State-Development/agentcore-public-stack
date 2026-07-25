"""User pin state for the Agent Marketplace (D8, D9 user side — Phase 5).

A pin is a **pointer, never a fork** (D8). Agents are addressable by id and already run
for anyone with access, so there is nothing to install: "Add to my agents" stores an id.
Forking would duplicate the record (version drift the moment the author updates) and
re-owner the bindings, which silently breaks Skills v2 invoke-through in the confusing
direction.

Storage is one item per user on the **user-settings** table, following the established
``USER#{id}`` + uppercase-semantic-SK convention (``SETTINGS``, ``TOOL_PREFERENCES``):

    PK = USER#{user_id}, SK = "PINNED_AGENTS"
    { pinned: [ { agentId, order, pinnedAt } ], dismissed: [ agentId, ... ] }

Two properties worth stating because both are easy to erode:

**1. ``dismissed`` is written from Phase 5, before anything reads it.** Role-seeded pins
(D9, Phase 6) are resolved live rather than materialized, so a seeded pin the user removed
would re-apply on the very next resolution without a tombstone. Recording the tombstone
now means the resolver that arrives with role pins inherits a real history instead of
treating every existing user as having dismissed nothing.

**2. Pinning clears the tombstone; unpinning writes one.** They are two halves of one
toggle. A user who pins an Agent they previously dismissed has changed their mind, and
leaving the tombstone in place would make a future role seed for that Agent unreachable
forever — the failure being tombstoned exists to prevent, inverted.

The write is a read-modify-write on a single per-user item. Two taps racing can lose one
of them; the cost is a pin the user re-taps, and the alternative (a set-typed attribute
with atomic ADD/DELETE) cannot carry the per-entry ``order`` and ``pinnedAt`` the shelf
sorts and renders by.
"""

import logging
import os
from datetime import datetime, timezone

from .models import PinnedAgentRef, UserPinState

logger = logging.getLogger(__name__)

_SK = "PINNED_AGENTS"

# A user with hundreds of pins has a sidebar nobody can read, and the resolver reads one
# Agent record per pin. Neither is a hard system limit; this is the point where the
# feature has stopped being a shelf, and refusing is kinder than degrading silently.
MAX_PINS = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat() + "Z"


def _table():
    import boto3

    table_name = os.environ.get("DYNAMODB_USER_SETTINGS_TABLE_NAME")
    if not table_name:
        raise RuntimeError("DYNAMODB_USER_SETTINGS_TABLE_NAME environment variable is required")
    return boto3.resource("dynamodb").Table(table_name)


def _key(user_id: str) -> dict:
    return {"PK": f"USER#{user_id}", "SK": _SK}


class PinLimitError(ValueError):
    """Raised when a pin would take the user past ``MAX_PINS``."""


async def get_pin_state(user_id: str) -> UserPinState:
    """The user's raw pin item, or an empty state when they have never pinned anything."""
    item = _table().get_item(Key=_key(user_id)).get("Item")
    if not item:
        return UserPinState()
    return UserPinState.model_validate(
        {"pinned": item.get("pinned", []), "dismissed": item.get("dismissed", [])}
    )


async def _put_pin_state(user_id: str, state: UserPinState) -> UserPinState:
    _table().put_item(
        Item={
            **_key(user_id),
            "pinned": [ref.model_dump(by_alias=True, exclude_none=True) for ref in state.pinned],
            "dismissed": state.dismissed,
            "updatedAt": _now(),
        }
    )
    return state


async def add_pin(user_id: str, agent_id: str) -> UserPinState:
    """Pin an Agent for this user, and clear any dismissal of it.

    Idempotent: pinning something already pinned keeps the original ``pinnedAt`` and
    order, so a double tap does not reshuffle the shelf.
    """
    state = await get_pin_state(user_id)

    if any(ref.agent_id == agent_id for ref in state.pinned):
        if agent_id not in state.dismissed:
            return state
    elif len(state.pinned) >= MAX_PINS:
        raise PinLimitError(
            f"You have reached the limit of {MAX_PINS} pinned agents. Remove one to add another."
        )
    else:
        next_order = max((ref.order for ref in state.pinned), default=-1) + 1
        state.pinned.append(
            PinnedAgentRef(agent_id=agent_id, order=next_order, pinned_at=_now())
        )

    state.dismissed = [pinned_id for pinned_id in state.dismissed if pinned_id != agent_id]
    logger.info(f"📌 {user_id} pinned agent {agent_id}")
    return await _put_pin_state(user_id, state)


async def remove_pin(user_id: str, agent_id: str) -> UserPinState:
    """Unpin an Agent and remember the dismissal (D9.3).

    The tombstone is written even when the pin was the user's own rather than a role
    seed: the two are indistinguishable to the user, and a dismissal that only counted
    for seeded pins would let a role pin resurrect an Agent the user had already removed
    by hand.
    """
    state = await get_pin_state(user_id)
    state.pinned = [ref for ref in state.pinned if ref.agent_id != agent_id]
    if agent_id not in state.dismissed:
        state.dismissed.append(agent_id)
    logger.info(f"📌 {user_id} unpinned agent {agent_id}")
    return await _put_pin_state(user_id, state)
