"""Which Agent version a step conversation's turn is pinned to (``workflow-designer.md`` §23.3).

A saved workflow's Agent step runs the version its run recorded, for everyone, the Agent's owner
included. Every non-resume turn re-resolves the Agent, so the pin lives on the step conversation,
not on one request:

- **The step's first turn** takes the handoff, and its request carries ``handoff_context.agentVersion``.
  Acceptance holds that number to the ``HANDOFF#`` row's ``agentVersion`` and ends the turn before
  any model call when they differ (``HandoffTurn.version_refused``), so the client can't pick one.
- **Every later turn** reads ``pinnedAgentVersion`` from the step conversation's ``META`` row, which
  the turn path already read (B1), and ignores any version on the request. The rule is re-checked
  by ``resolve_invocation_agent`` each time, so a takedown between turns blocks the next one.

Pure: no IO. The flag is read here (``agent_version_pinning_enabled``), so off is exactly today.
A resume never calls this: it replays its ``PausedTurnSnapshot`` and resolves nothing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Mapping, Optional

from apis.shared.feature_flags import agent_version_pinning_enabled

if TYPE_CHECKING:
    from apis.shared.handoff.turn import HandoffContextRequest, HandoffTurn

logger = logging.getLogger(__name__)

#: The step conversation's ``META`` attribute (``set_session_spawned_from``).
PINNED_AGENT_VERSION_ATTR = "pinnedAgentVersion"


def meta_pinned_version(session_row: Optional[Mapping[str, Any]]) -> Optional[int]:
    """``pinnedAgentVersion`` on a session row as an int, or ``None`` (absent or malformed)."""
    raw = (session_row or {}).get(PINNED_AGENT_VERSION_ATTR)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return None
    return number if number >= 1 and number == raw else None


def _bound_agent_id(session_row: Optional[Mapping[str, Any]]) -> Optional[str]:
    prefs = (session_row or {}).get("preferences")
    if not isinstance(prefs, Mapping):
        return None
    bound = prefs.get("assistantId")
    return bound if isinstance(bound, str) and bound else None


def turn_pinned_version(
    *,
    handoff_turn: Optional["HandoffTurn"],
    handoff_context: Optional["HandoffContextRequest"],
    session_row: Optional[Mapping[str, Any]],
    agent_id: Optional[str],
    is_agent_mention: bool,
) -> Optional[int]:
    """The version this turn's Agent resolves to as a pin, or ``None`` for the unpinned path.

    The conversation's pin wins over the request's (a later turn ignores what the request says).
    It applies only to the Agent the conversation is bound to: an ``@``-mention of another Agent in
    a step conversation is not that step, so it runs unpinned. The request's pin counts only on a
    turn that takes a handoff (``handoff_turn.request``), only while the flag is on.
    """
    requested = handoff_context.agent_version if handoff_context is not None else None
    if not agent_version_pinning_enabled():
        return None
    if not agent_id:
        if requested is not None:
            logger.warning("handoff_context.agentVersion ignored: the turn has no Agent")
        return None

    pinned = meta_pinned_version(session_row)
    if pinned is not None:
        bound = _bound_agent_id(session_row)
        if bound == agent_id or (bound is None and not is_agent_mention):
            if requested is not None and requested != pinned:
                logger.warning(
                    "handoff_context.agentVersion ignored: the conversation is pinned to version %s",
                    pinned,
                )
            return pinned
        logger.info("Pinned step conversation: a turn on another Agent runs unpinned")

    if handoff_turn is not None and handoff_turn.request is not None:
        return handoff_turn.requested_version
    if requested is not None:
        logger.warning("handoff_context.agentVersion ignored: this turn takes no handoff")
    return None


__all__ = ["PINNED_AGENT_VERSION_ATTR", "meta_pinned_version", "turn_pinned_version"]
