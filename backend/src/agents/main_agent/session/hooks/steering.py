"""Mid-turn steering: inject a queued follow-up at the next tool boundary.

See ``docs/specs/mid-turn-steering.md``. PR #916 made Enter mean "say this"
while a response streams, but the follow-up sits in the composer until the turn
ends. This hook lands it at the next tool boundary instead, so the agent reads
it *before* choosing its next action — the user who sees the wrong file being
opened no longer has to choose between paying for the rest of a doomed turn and
stopping it (which discards a partial generation and re-establishes the prefix,
the more expensive of the two).

Two Strands events, and the split between them is the whole correctness story:

``AfterToolsEvent``
    Fires with the assembled tool-result message **before** it is appended to
    history, so appending a ``{"text": ...}`` block puts the user's words into
    the same user-role message that carries the tool results. Valid Bedrock
    Converse shape, persists through the normal ``append_message`` path, and
    append-only against the cached prefix — the injection lands inside the
    segment the ``strategy="auto"`` message-level cachePoint covers, behind
    both static points, so the next model call still reads the stable prefix
    from cache.

``MessageAddedEvent``
    Fires from ``Agent._append_messages``, which is the first point at which
    the injection is really in the conversation. Only there is the inbox entry
    consumed.

That split exists because ``AfterToolsEvent`` fires from a ``finally`` and so
*also* fires on the cancel, error, and interrupt paths. On the interrupt path
``_stop_for_interrupts`` runs and ``_append_messages`` is never reached: the
message this hook just mutated is discarded. A hook that consumed on read would
therefore destroy the user's words on every steer that happened to land on the
same tool batch as an OAuth consent or an approval prompt — silent data loss,
low frequency, very hard to reproduce. So the hook peeks, and commits on
append. If the turn ends with entries unconsumed, the lease row is deleted with
the lease and the SPA's un-acked queue entries flush the PR #916 way.

**SDK-boundary caveat.** ``HookEvent.__setattr__`` is write-guarded by
``_can_write``, which for ``AfterToolsEvent`` allows only ``end_turn``.
Mutating the message dict *in place* is not blocked, but it is also not
explicitly sanctioned. ``tests/agents/main_agent/session/test_steering_hook.py``
carries a contract test that asserts the mutation still reaches
``agent.messages``; it is the canary for a ``strands-agents`` bump.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from strands.hooks import AfterToolsEvent, HookProvider, HookRegistry, MessageAddedEvent

logger = logging.getLogger(__name__)

# The injected text is framed so the model reads it as the user speaking during
# the turn, rather than as tool output or as its own scratch notes.
STEER_OPEN_TAG = "<user_message_during_turn>"
STEER_CLOSE_TAG = "</user_message_during_turn>"


def wrap_steering_text(texts: List[str]) -> str:
    """Frame queued follow-ups as one tagged block, in arrival order."""
    body = "\n\n".join(text.strip() for text in texts if text and text.strip())
    return f"{STEER_OPEN_TAG}\n{body}\n{STEER_CLOSE_TAG}"


class SteeringHook(HookProvider):
    """Inject queued follow-ups into the running turn at each tool boundary.

    Holds no per-turn state beyond the in-flight injection it must ack: the
    lease is read off the session manager on every boundary (per the CLAUDE.md
    rule that per-session state is never cached on an agent instance — the
    cached agent outlives the turn, and an ``@``-mention turn builds a second
    ``Agent`` with its own manager and its own hook).

    Fail-soft in every direction. Any error degrades to PR #916's end-of-turn
    flush; the user's text is either injected exactly once or sent as a normal
    turn, never both and never neither.
    """

    def __init__(self, session_manager: Any):
        self.session_manager = session_manager
        # The message object this hook last mutated, and the entry ids inside
        # it. Identity, not equality: the dict Strands appends is the same
        # object we appended to, and a discarded (interrupt-path) message is
        # simply overwritten by the next boundary's injection.
        self._pending_message: Optional[dict] = None
        self._pending_entry_ids: List[dict] = []
        # Entry ids whose injection is confirmed in history, drained by the
        # stream coordinator to emit `steering_applied`.
        self._applied: List[dict] = []

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterToolsEvent, self.inject_pending_steering)
        registry.add_callback(MessageAddedEvent, self.commit_pending_steering)

    def drain_applied(self) -> List[dict]:
        """Take the injections confirmed since the last drain."""
        applied, self._applied = self._applied, []
        return applied

    # -- injection ---------------------------------------------------------

    async def inject_pending_steering(self, event: AfterToolsEvent) -> None:
        """Append the user's queued follow-ups to this batch's tool results."""
        from apis.shared.feature_flags import mid_turn_steering_enabled

        if not mid_turn_steering_enabled():
            return

        content = event.message.get("content")
        if not content:
            # Nothing was committed this batch (cancelled before any tool ran).
            # There is no message to ride, and one will not be appended.
            return

        lease = getattr(self.session_manager, "turn_lease", None)
        if lease is None:
            return

        try:
            from apis.shared.sessions.session_lease import peek_steer_queue

            entries = await peek_steer_queue(lease)
        except Exception:
            logger.warning("Steering peek failed; leaving the follow-up queued", exc_info=True)
            return

        if not entries:
            return

        texts = [str(entry.get("text", "")) for entry in entries]
        content.append({"text": wrap_steering_text(texts)})

        self._pending_message = event.message
        self._pending_entry_ids = [
            {"id": str(entry.get("id")), "text": str(entry.get("text", ""))}
            for entry in entries
        ]
        logger.info(
            "Injected %d steering message(s) at a tool boundary for session %s",
            len(entries),
            lease.session_id,
        )

    # -- commit ------------------------------------------------------------

    async def commit_pending_steering(self, event: MessageAddedEvent) -> None:
        """Consume the inbox entries now that their message is in history.

        Identity-matched against the message this hook mutated: every other
        message added this turn (the user's own turn, assistant messages, tool
        results with no injection) falls through untouched.
        """
        if self._pending_message is None or event.message is not self._pending_message:
            return

        pending, self._pending_message = self._pending_entry_ids, None
        self._pending_entry_ids = []

        lease = getattr(self.session_manager, "turn_lease", None)
        if lease is None:
            return

        from apis.shared.sessions.session_lease import clear_steer_entry

        for entry in pending:
            try:
                await clear_steer_entry(lease, entry["id"])
            except Exception:
                # A failed clear re-delivers at the next boundary; the entry id
                # makes the SPA's ack idempotent. Losing the text would not be.
                logger.warning("Steering entry clear failed", exc_info=True)
                continue
            self._applied.append(entry)
