"""Persist the user's own words as soon as their turn enters history.

The prompt that reaches the model is often not the prompt the user typed. RAG
prepends retrieved context, attachments add guidance, an embedded MCP App
pushes a context block, and an interrupted previous turn prepends an
``<interruption_note>`` addressed to the model. All of that is deliberately
kept in persisted history — it is an honest record of what the model actually
read — and the UI is supposed to show the clean original instead, via the
``displayText`` (``D#``) record this hook writes.

**Why a hook, and why this event.** That write used to live at the very end of
``stream_coordinator.stream_response``, in the success path. Nothing on the
Stop, disconnect, or error paths wrote it, so any turn that did not reach that
final line left the raw augmented prompt as the only thing the UI could
render — and a turn is at its most likely to be interrupted precisely when it
is carrying an interruption note, because the note only exists because the
*previous* turn was interrupted. The visible result was the model-directed
note sitting in the user's own chat bubble, permanently. It also showed
transiently on any reload mid-turn, for every augmentation.

``MessageAddedEvent`` fires from ``Agent._append_messages``, the moment the
user's turn is really in the conversation — before the model call, so every
later exit path (completion, Stop, cancellation, error, container death)
already has the record written. That is the whole point: the write no longer
depends on how the turn ends.

**Why not simply write at request start.** If the turn died before the user
message was appended, a record keyed to that index would be inherited by
whatever message later takes the index — showing one turn's clean text on a
different turn's bubble. Anchoring to the actual append makes the index and
the record land together.

**One-shot per turn, and why role alone is not enough.** Tool-result messages
are also role ``user`` under Bedrock Converse, and mid-turn steering appends
into them. The hook is armed once per turn and disarms on the first user-role
message it writes, which is the user's prompt — tool results only exist after
the first model call.

Armed unconditionally at the head of every turn, *including to ``None``*, for
the same reason ``turn_lease`` is stamped unconditionally: the agent instance
is cached and outlives the turn (#741/#751), so an arm left behind by a
previous turn would fire against the wrong one.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from strands.hooks import HookProvider, HookRegistry, MessageAddedEvent

logger = logging.getLogger(__name__)


class DisplayTextHook(HookProvider):
    """Write the turn's ``displayText`` when the user message is appended.

    Best-effort in every direction: ``displayText`` is a UI nicety, and a
    failure here must never break a turn. When it does fail, the stream
    coordinator's end-of-turn write is still there as a backstop for turns
    that complete.
    """

    def __init__(self) -> None:
        self._armed: Optional[dict] = None
        self._written = False

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(MessageAddedEvent, self.write_display_text)

    @property
    def wrote_this_turn(self) -> bool:
        """Whether this turn's record is already stored.

        Read by the stream coordinator so its end-of-turn backstop doesn't
        repeat a write this hook already made.
        """
        return self._written

    def arm(
        self,
        *,
        session_id: str,
        user_id: str,
        message_index: int,
        display_text: Optional[str],
    ) -> None:
        """Prime the hook for one turn, or clear it when there's nothing to write.

        ``display_text`` is the user's original message, passed only when the
        prompt was modified before reaching the model. A turn that sends the
        user's text verbatim (and a resume / continuation, which sends no new
        user turn at all) passes ``None`` and disarms.
        """
        self._written = False
        if not display_text:
            self._armed = None
            return
        self._armed = {
            "session_id": session_id,
            "user_id": user_id,
            "message_id": message_index,
            "display_text": display_text,
        }

    async def write_display_text(self, event: MessageAddedEvent) -> None:
        """Store the clean text once the user's message is in history."""
        armed = self._armed
        if armed is None:
            return

        message = getattr(event, "message", None) or {}
        if message.get("role") != "user":
            return
        # Not every role-`user` message is the user speaking. Tool results
        # carry that role under Bedrock Converse, and Strands prepends a
        # SYNTHETIC tool-result message ahead of the prompt when history ends
        # on a dangling `toolUse` (`Agent._run_loop`, "appending a toolResult
        # message to have valid conversation") — which is precisely the shape
        # an interrupted tool turn leaves behind, i.e. the case this hook
        # exists for. Consuming the arm there would stamp the clean text onto
        # the repair message instead of the user's own.
        if any(
            isinstance(block, dict) and ("toolResult" in block or "toolUse" in block)
            for block in message.get("content") or []
        ):
            return

        # One-shot: consume before the await so a tool-result message later in
        # the same turn can never re-enter this.
        self._armed = None

        try:
            from apis.shared.sessions.metadata import store_user_display_text

            await store_user_display_text(**armed)
            self._written = True
            logger.info(
                "💾 Stored displayText for user message %s at append time",
                armed["message_id"],
            )
        except Exception:  # noqa: BLE001 - a UI nicety must never break a turn
            logger.error(
                "Failed to store displayText for user message %s",
                armed["message_id"],
                exc_info=True,
            )
