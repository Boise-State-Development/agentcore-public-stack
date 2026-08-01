"""Which Agent configuration a caller actually runs, and which one they are shown (§4).

One question, one answer, one place. ``resolve_invocation_agent`` takes the live Agent
record an access check has already admitted, and returns the ``Assistant`` this caller
should run:

| Caller                                        | Gets                     |
|-----------------------------------------------|--------------------------|
| Anyone who pinned it or opened it from the store | The published snapshot |
| The **owner**                                 | Their own draft          |
| Anyone, when nothing is published              | The live record          |

``resolve_display_agent`` answers the same question for the marketplace **detail read**,
and differs on exactly one row: editors see the draft too. The two are kept side by side
here rather than merged, because the reason they differ is easy to lose and expensive to
get wrong in either direction — see that function's docstring.

This is deliberately *not* in ``versions.py`` (pure, no I/O) or in
``version_repository.py`` (persistence). Deciding which version a person runs is policy,
and policy that reads like this — a short function with the whole table in view — is much
harder to get subtly wrong than the same three branches spread across a route handler.

**Two things it is not.**

It is **not an access check.** The caller has already been admitted by
``get_assistant_with_access_check``; this only chooses *which configuration* to run. It
never widens or narrows who may reach an Agent, which is why ``AgentVersion`` carries no
``visibility`` and why the overlay in ``versions.apply_version`` leaves identity fields on
the live record.

It is **not a fallback to the draft on error.** A published Agent whose snapshot cannot be
read raises. Silently serving the draft instead would reintroduce the exact hole this epic
closed — unreviewed instructions reaching a pinned user — and it would do it precisely when
something is already wrong. Failing the turn is the safe direction.
"""

import logging
from typing import Optional, Tuple

from .models import Assistant
from .version_repository import get_version
from .versions import apply_version

logger = logging.getLogger(__name__)


class AgentVersionUnavailableError(RuntimeError):
    """A published Agent's snapshot could not be loaded.

    Raised rather than degraded. See the module docstring: falling back to the draft here
    would serve unreviewed instructions to whoever tapped a store tile.
    """

    def __init__(self, agent_id: str, number: int):
        self.agent_id = agent_id
        self.number = number
        super().__init__(
            f"Version {number} of agent {agent_id} is published but could not be loaded."
        )


def runs_own_draft(assistant: Assistant, user_id: Optional[str]) -> bool:
    """Whether this caller runs the Agent's editable draft rather than the published one.

    ⚠️ **Owner identity, not edit access.** An editor (share permission ``editor``) can
    change an Agent's instructions but does *not* get to run the unpublished result — the
    spec is explicit that this must be "owner identity, not 'anyone with edit access'".
    Widening it to editors would mean a share grant quietly became a way to bypass review,
    which is the same shape of mistake as letting ``publisherId`` gate access.

    The owner running their own draft is not a loophole: it is the only way to iterate
    before resubmitting, and it affects nobody else. It does need to be *visible* in the UI
    that they are running an unpublished draft, which is a surface concern, not this one.
    """
    return bool(user_id) and assistant.owner_id == user_id


async def resolve_invocation_agent(
    assistant: Assistant, user_id: Optional[str]
) -> Tuple[Assistant, Optional[int]]:
    """Return ``(assistant_to_run, version_number)`` for this caller.

    ``version_number`` is ``None`` when the live record is what runs — either because the
    caller owns it, or because nothing is published. It is returned for logging and for the
    SPA to label what ran; it is deliberately **not** threaded into the agent cache key. The
    key is built from construction *values*, all of which a version already changes, so the
    number buys no discrimination — and adding it breaks resume, because the paused-turn
    snapshot does not carry it. ``chat/routes.py`` carries the full reasoning at the
    ``resolved_version`` declaration; §4.2 of the spec records why the original design note
    saying the opposite was reverted.

    Raises ``AgentVersionUnavailableError`` if a published version is named but missing.
    """
    listing = assistant.listing
    published = listing.published_version if listing else None

    if published is None:
        # The overwhelmingly common case, and the one that must not change: an Agent that
        # was never submitted, or is still private/in-review, has no snapshot and runs
        # exactly as it did before this feature existed.
        return assistant, None

    if runs_own_draft(assistant, user_id):
        logger.info(
            f"🧪 Agent {assistant.assistant_id} running the owner's draft "
            f"(published version is {published})"
        )
        return assistant, None

    version = await get_version(assistant.assistant_id, published)
    if version is None:
        raise AgentVersionUnavailableError(assistant.assistant_id, published)

    logger.info(f"📌 Agent {assistant.assistant_id} running published version {published}")
    return apply_version(assistant, version), published


async def resolve_display_agent(
    assistant: Assistant, *, can_edit: bool
) -> Tuple[Assistant, Optional[int]]:
    """Return ``(assistant_to_show, version_number)`` for the marketplace **detail read**.

    The same question ``resolve_invocation_agent`` answers, for the page rather than the
    turn — and it has to be answered, because the detail read is where a store user decides
    whether to open an Agent. Serving the author's draft there means the page describes an
    unreviewed configuration while invocation runs the approved one: a different name, a
    different summary, and — since ``capabilities`` is resolved from ``bindings`` — a list
    of tools the published Agent does not actually have.

    **One row differs from invocation, deliberately: editors see the draft.** For a turn the
    line is owner-only, because an editor *running* the unpublished result would turn a
    share grant into a way around review. For a read the opposite is required — the Agent
    Designer loads this same endpoint, so handing an editor the published snapshot would
    populate the edit form with the approved copy and their next save would silently revert
    the owner's draft. ``can_edit`` is therefore the ``INSTRUCTIONS_PERMISSIONS`` set the
    route already computes: the people trusted with the system prompt are exactly the people
    who must see the draft.

    Passed as a bool rather than the permission string so this module keeps knowing nothing
    about the route's vocabulary.

    Raises ``AgentVersionUnavailableError`` if a published version is named but missing —
    the same refusal as invocation, for the same reason. Such an Agent has no store tile
    either (the index is sparse on the version row), so this is only reachable by direct
    link, and answering it with unreviewed content is the one thing worth avoiding.
    """
    listing = assistant.listing
    published = listing.published_version if listing else None

    if published is None or can_edit:
        return assistant, None

    version = await get_version(assistant.assistant_id, published)
    if version is None:
        raise AgentVersionUnavailableError(assistant.assistant_id, published)

    return apply_version(assistant, version), published
