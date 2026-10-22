"""The ``HANDOFF#`` row and its states (``docs/specs/agent-handoffs-and-consults.md`` §4.1–§4.3).

A handoff lives on its **source** session. Its states::

    open ─┬─▶ running ⇄ needs_input
          │      │  ╲       │
          │      ▼   ▼      ▼
          │    stopped ──▶ done      (stopped follows the target's next turn; done is frozen)
          └─▶ dismissed

``abandoned`` is never stored: an ``open`` row older than ``ABANDON_AFTER`` reads as abandoned,
so no sweep job is needed.

Status writes after acceptance happen after the target's ``done`` block (PR-0.2), which is why a
``needs_input`` row can go straight to ``done`` or ``stopped``: the intermediate ``running`` is
live state in the SPA, not a write. Once ``done``, the target conversation is the user's own and
the row stops following it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, FrozenSet, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

HandoffKind = Literal["mention", "suggested", "send_back"]
HandoffStatus = Literal["open", "running", "needs_input", "done", "stopped", "dismissed"]
EffectiveHandoffStatus = Literal["open", "running", "needs_input", "done", "stopped", "dismissed", "abandoned"]

ABANDON_AFTER = timedelta(hours=24)
#: What the user typed for the Agent, minus the mention.
HANDOFF_REQUEST_MAX_CHARS = 20_000
#: Display-only text on the row (titles, the live status line on replay).
HANDOFF_DISPLAY_MAX_CHARS = 240

#: Allowed moves, by the status being left. ``needs_input`` and ``stopped`` may rewrite
#: themselves (a second pause with a new ``waitingOn``); ``running`` may not, because the move
#: into it from ``open`` is acceptance and must happen once (``HandoffStore.accept``).
TRANSITIONS: Dict[str, FrozenSet[str]] = {
    "open": frozenset({"running", "dismissed"}),
    "running": frozenset({"needs_input", "done", "stopped"}),
    "needs_input": frozenset({"running", "needs_input", "done", "stopped"}),
    "stopped": frozenset({"running", "needs_input", "done", "stopped"}),
    "done": frozenset(),
    "dismissed": frozenset(),
}


def sources_of(status: str) -> FrozenSet[str]:
    """Every status a row may be in for a move to ``status`` to be allowed."""
    return frozenset(src for src, targets in TRANSITIONS.items() if status in targets)


class TurnsCovered(BaseModel):
    """The source turns an excerpt covers, counted over the whole conversation."""

    model_config = ConfigDict(populate_by_name=True)

    first: int
    last: int
    of: int
    omitted: int = 0


class Handoff(BaseModel):
    """One ``HANDOFF#<handoffId>`` row, keyed under its owner (``PK=USER#<userId>``)."""

    model_config = ConfigDict(populate_by_name=True)

    handoff_id: str = Field(..., alias="handoffId")
    user_id: str = Field(..., alias="userId")
    session_id: str = Field(..., alias="sessionId", description="The source conversation")
    kind: HandoffKind
    status: HandoffStatus = "open"
    agent_id: str = Field(..., alias="agentId")
    agent_name: str = Field("", alias="agentName")
    request: str = ""
    source_title: str = Field("", alias="sourceTitle", description="The title the wrapper names, fixed at create")
    source_message_index: int = Field(
        -1, alias="sourceMessageIndex", description="Index of the message the handoff row sits after; -1 for none"
    )
    excerpt_hash: Optional[str] = Field(None, alias="excerptHash")
    excerpt_tokens: int = Field(0, alias="excerptTokens")
    turns_covered: Optional[TurnsCovered] = Field(None, alias="turnsCovered")
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")
    target_session_id: Optional[str] = Field(None, alias="targetSessionId")
    context_edited: Optional[bool] = Field(None, alias="contextEdited")
    context_removed: Optional[bool] = Field(None, alias="contextRemoved")
    status_line: Optional[str] = Field(None, alias="statusLine")
    waiting_on: Optional[str] = Field(None, alias="waitingOn")
    stopped_reason: Optional[str] = Field(None, alias="stoppedReason")

    def effective_status(self, now: Optional[datetime] = None) -> EffectiveHandoffStatus:
        """The status as read: an ``open`` row past ``ABANDON_AFTER`` is ``abandoned``."""
        if self.status != "open":
            return self.status
        try:
            created = datetime.fromisoformat(self.created_at)
        except ValueError:
            return self.status
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if (now or datetime.now(timezone.utc)) - created >= ABANDON_AFTER:
            return "abandoned"
        return self.status


__all__ = [
    "ABANDON_AFTER",
    "EffectiveHandoffStatus",
    "HANDOFF_DISPLAY_MAX_CHARS",
    "HANDOFF_REQUEST_MAX_CHARS",
    "Handoff",
    "HandoffKind",
    "HandoffStatus",
    "TRANSITIONS",
    "TurnsCovered",
    "sources_of",
]
