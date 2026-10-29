"""Where a forked conversation came from (docs/specs/conversation-rewind-and-fork.md §7).

A fork is a new session holding a copy of everything before one of the
parent's user messages. The copy is what makes the fork durable: it survives
the parent's deletion and restores like any other session. This record is what
makes the fork's first turn cheap: the Runtime uses it to find the parent's
live history in the same container and start the fork from an exact copy of
the bytes the parent sent, which is what the prompt cache holds. A cold
restore cannot reproduce those bytes (long-term-memory context is sent but
never stored, §2.3).

Pure: no I/O. app-api writes it on the child's session row; the Runtime reads
it from the row its restore already loads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

FORKED_FROM_ATTRIBUTE = "forkedFrom"
ROOT_SESSION_ATTRIBUTE = "rootSessionId"


@dataclass(frozen=True)
class ForkOrigin:
    """The parent and the cut.

    ``message_index`` is the parent's physical index of the user message the
    fork was cut before (for display and the cost marker). ``cut`` is the
    logical length the fork starts with, which is also how many messages were
    copied. ``parent_rewind_epoch`` is the rewind state the cut was computed
    against: a parent list that reflects a different one is not the list the
    cut describes.
    """

    session_id: str
    message_index: int
    cut: int
    parent_rewind_epoch: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "messageIndex": self.message_index,
            "cut": self.cut,
            "parentRewindEpoch": self.parent_rewind_epoch,
        }

    @classmethod
    def from_row(cls, row: Optional[Dict[str, Any]]) -> Optional["ForkOrigin"]:
        """The origin on a session row, or None for a session that is not a fork."""
        raw = (row or {}).get(FORKED_FROM_ATTRIBUTE)
        if not isinstance(raw, dict) or not isinstance(raw.get("sessionId"), str):
            return None
        try:
            return cls(
                session_id=raw["sessionId"],
                message_index=int(raw.get("messageIndex", 0)),
                cut=int(raw.get("cut", 0)),
                parent_rewind_epoch=int(raw.get("parentRewindEpoch", 0) or 0),
            )
        except (TypeError, ValueError):
            return None


def root_session_of(session_id: str, row: Optional[Dict[str, Any]]) -> str:
    """The conversation a session's family started from: its own id unless it is a fork."""
    root = (row or {}).get(ROOT_SESSION_ATTRIBUTE)
    return root if isinstance(root, str) and root else session_id
