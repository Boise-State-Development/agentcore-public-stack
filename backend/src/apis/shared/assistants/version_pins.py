"""Which Agent versions a saved workflow's step may pin (``workflow-designer.md`` §23.2).

A workflow's Agent step records the version it was added or last moved on at (``@ <n>``). With
``AGENT_VERSION_PINNING_ENABLED`` on, the step runs that version for everyone who starts it, the
Agent's owner included, but only while the version may still run:

1. the Agent has a published version ``P``,
2. ``n ≤ P``, and
3. ``n`` is ``P``, or ``n``'s ``VERSION#`` row carries ``publishedAt``.

So an approved update (v3 → v5) leaves a v3 step running; an admin rollback (v5 → v3) blocks steps
pinned to v4 or v5 until it rolls forward; a takedown or a withdrawal to private blocks every pinned
step, rather than falling forward to the author's unreviewed draft; and a rejected submission, whose
number was cut but never published, can never run.

**One copy of the rule.** The sheet (``apis.shared.workflows.sheet``), run create (app-api's
``workflow_runs``) and the turn (``version_resolution.resolve_invocation_agent``) all call
:func:`pin_refusal`. Nothing else decides it.

Pure: no I/O, no flag. Callers read the row (when :func:`pin_row_needed` says to) and decide whether
pinning is on.
"""

from __future__ import annotations

from typing import Literal, Optional

from .models import AgentVersion

PinRefusalReason = Literal["unpublished", "above_published", "never_published", "missing"]

#: Every reason :func:`pin_refusal` can give, in the order it checks them.
PIN_REFUSAL_REASONS: tuple = ("unpublished", "above_published", "never_published", "missing")


class AgentPinRefusedError(RuntimeError):
    """A pinned version that the rule refuses.

    Deliberately **not** an ``AgentVersionUnavailableError`` (a 503: a published snapshot that
    could not be read). This is a decision, answered in the conversation with
    :func:`pin_refusal_message`, never an outage.
    """

    def __init__(
        self,
        agent_id: str,
        number: int,
        reason: PinRefusalReason,
        *,
        published: Optional[int] = None,
    ) -> None:
        self.agent_id = agent_id
        self.number = number
        self.reason = reason
        self.published = published
        super().__init__(f"Version {number} of agent {agent_id} can't run as a pinned step: {reason}")


def pin_row_needed(number: int, published: Optional[int]) -> bool:
    """Whether :func:`pin_refusal` needs version ``number``'s row to decide.

    False when the answer is already known from the pointer alone: nothing is published, or the pin
    is above it. A caller that serves the snapshot reads the row anyway.
    """
    return published is not None and number <= published


def pin_refusal(
    number: int, published: Optional[int], version: Optional[AgentVersion]
) -> Optional[PinRefusalReason]:
    """Why version ``number`` may not run as a pin, or ``None`` when it may.

    ``published`` is the Agent's current ``listing.publishedVersion``. ``version`` is the
    ``VERSION#`` row for ``number``, or ``None`` when it was not read or does not exist; it is
    consulted only when :func:`pin_row_needed`, and then ``None`` means ``missing``.
    """
    if published is None:
        return "unpublished"
    if number > published:
        return "above_published"
    if version is None:
        return "missing"
    if number == published:
        return None
    return None if version.published_at else "never_published"


def pin_refusal_message(reason: PinRefusalReason, number: int, published: Optional[int]) -> str:
    """What the step conversation says when its pinned version can't run (the Runtime's copy).

    Written for the person in the step conversation, who may not be the workflow's editor: it
    names the version, says why, and says who can move the step on.
    """
    if reason == "unpublished":
        return (
            f"This Agent is no longer published, so version {number}, the version this workflow "
            "step runs, can't run. Ask whoever edits the workflow to remove the step or choose "
            "another Agent."
        )
    if reason == "above_published" and published is not None:
        return (
            f"Version {number} of this Agent is no longer published; version {published} is the "
            f"current one. Start the run again and use version {published}, or ask whoever edits "
            "the workflow to move the step on."
        )
    if reason == "never_published" and published is not None:
        return (
            f"Version {number} of this Agent was never published, so this workflow step can't run "
            f"it. Version {published} is the current published version: start the run again and "
            f"use version {published}, or ask whoever edits the workflow to move the step on."
        )
    return (
        f"Version {number} of this Agent can't be found, so this workflow step can't run. Ask "
        "whoever edits the workflow to remove the step or choose another Agent."
    )


__all__ = [
    "PIN_REFUSAL_REASONS",
    "AgentPinRefusedError",
    "PinRefusalReason",
    "pin_refusal",
    "pin_refusal_message",
    "pin_row_needed",
]
