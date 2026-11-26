"""Session prewarm's prepare mode: build the next turn's agent ahead of the send.

A plain warm call (``warm: true``) only starts the microVM. A prepare call (``warm`` and
``prepare`` together, docs/specs/turn-path-ttft.md) also carries the selection the next send
would use, and the route runs the turn's own phases with it, in the same order, then builds
the agent into the agent cache and returns. The send that follows finds its agent built.

What a prepare never does: take the single-flight lease, write a session row, charge quota,
search a knowledge base, or call a model. app-api's ``POST /chat/prewarm`` is the gate
(``SESSION_PREWARM_PREPARE_ENABLED``); the Runtime has no flag of its own, because the
request field is the trigger.

This module holds the parts that are not route phases: the request allowlist, the outcome,
and the in-process record of prepares in flight, so a send that arrives while its agent is
being built waits for that build instead of starting a second one. The record is keyed on
``(user, session)``, never the session id alone (CLAUDE.md, "One session can be served by
more than one agent").
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: The request fields a prepare may carry. An allowlist, not a denylist: a warm call must
#: never be able to run a turn, resume one, dispatch an MCP App call or attach a file, and a
#: field added to ``InvocationRequest`` later stays out until someone decides it belongs here.
PREPARE_FIELDS: Tuple[str, ...] = (
    "session_id",
    "model_id",
    "temperature",
    "caching_enabled",
    "enabled_tools",
    "provider",
    "max_tokens",
    "inference_params",
    "rag_assistant_id",
    "agent_type",
    "enabled_skills",
    "selected_prompt_id",
    "incognito",
    "agent_mention",
    "system_prompt",
)

#: How long a send waits for a prepare that is building its agent. The build holds the event
#: loop, so in practice the send cannot run until it is over; this bounds the awaits around it.
IN_FLIGHT_WAIT_SECONDS = 30.0

#: Finished prepares remembered for the turn's ``preparedAgeMs`` property. One microVM serves
#: one conversation, so this only ever holds a handful.
_COMPLETED_MAX = 64

#: The two arms of the prepare A/B, as recorded on a turn. "treated" means a prepare was
#: attempted for the conversation (intent to treat: whatever its outcome); "holdout" means
#: app-api held one out and sent a plain warm in its place.
ARM_TREATED = "treated"
ARM_HOLDOUT = "holdout"

_in_flight: Dict[Tuple[str, str], "asyncio.Future[None]"] = {}
_completed_at: Dict[Tuple[str, str], float] = {}
_arms: Dict[Tuple[str, str], str] = {}


@dataclass(frozen=True)
class PrepareOutcome:
    """What a prepare did. ``reason`` says why nothing was built, and is None when one was."""

    prepared: bool
    reason: Optional[str] = None
    cache_hit: bool = False
    #: ``TurnPrelude.stage_summary()`` when the phases ran: log-only, never sent to the caller.
    stages: Optional[Dict[str, Any]] = None

    def body(self, elapsed_ms: int) -> Dict[str, Any]:
        return {
            "warmed": True,
            "prepared": self.prepared,
            "reason": self.reason,
            "cacheHit": self.cache_hit,
            "ms": elapsed_ms,
        }


def prepare_request(request: Any) -> Any:
    """``request`` reduced to the allowlist, as a new ``InvocationRequest``.

    app-api already forwards only these fields; this is the Runtime's own check, because the
    Runtime is what would run the turn.
    """
    kept = {name: getattr(request, name) for name in PREPARE_FIELDS if getattr(request, name, None) is not None}
    return type(request)(**kept)


def _key(user_id: str, session_id: str) -> Tuple[str, str]:
    return (user_id, session_id)


def note_arm(user_id: str, session_id: str, arm: str) -> None:
    """Remember which arm of the prepare A/B this conversation is in, for the turn's record.

    In process, like everything here: both arms start the microVM with the same warm call, so
    a conversation whose microVM is gone by the time it sends is missing from both alike.
    """
    if arm not in (ARM_TREATED, ARM_HOLDOUT):
        return
    key = _key(user_id, session_id)
    _arms.pop(key, None)
    while len(_arms) >= _COMPLETED_MAX:
        _arms.pop(next(iter(_arms)))
    _arms[key] = arm


def arm_of(user_id: str, session_id: str) -> Optional[str]:
    """ "treated", "holdout", or None when no prepare was asked for in this process."""
    return _arms.get(_key(user_id, session_id))


def begin(user_id: str, session_id: str) -> bool:
    """Record a prepare as in flight. False when one already is for this conversation."""
    key = _key(user_id, session_id)
    if key in _in_flight:
        return False
    note_arm(user_id, session_id, ARM_TREATED)
    _in_flight[key] = asyncio.get_running_loop().create_future()
    return True


def finish(user_id: str, session_id: str, *, prepared: bool) -> None:
    """Close the in-flight record and wake any send waiting on it. Never raises."""
    key = _key(user_id, session_id)
    future = _in_flight.pop(key, None)
    if future is not None and not future.done():
        future.set_result(None)
    if not prepared:
        return
    _completed_at.pop(key, None)
    while len(_completed_at) >= _COMPLETED_MAX:
        _completed_at.pop(next(iter(_completed_at)))
    _completed_at[key] = time.monotonic()


async def wait_for_in_flight(user_id: str, session_id: str) -> None:
    """Wait for this conversation's prepare, if one is building. Returns at once otherwise.

    No I/O: a dictionary lookup on the turn path. A prepare that fails or outlasts the bound
    just lets the send build its own agent, as it would have without one.
    """
    future = _in_flight.get(_key(user_id, session_id))
    if future is None:
        return
    try:
        await asyncio.wait_for(asyncio.shield(future), timeout=IN_FLIGHT_WAIT_SECONDS)
    except Exception:  # noqa: BLE001 - a prepare must never fail the turn that follows it
        logger.info("prepare wait ended without a result; the turn builds its own agent")


def prepared_age_ms(user_id: str, session_id: str) -> Optional[int]:
    """Milliseconds since this conversation's last finished prepare in this process, or None."""
    at = _completed_at.get(_key(user_id, session_id))
    if at is None:
        return None
    return max(0, int((time.monotonic() - at) * 1000))


def reset_for_tests() -> None:
    _in_flight.clear()
    _completed_at.clear()
    _arms.clear()
