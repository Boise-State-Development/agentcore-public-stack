"""Agent version snapshots — the capture/restore seam and the sort key.

Pure functions over ``Assistant`` and ``AgentVersion``. No I/O: ``version_repository``
turns a snapshot into a write-once DynamoDB item, and the service layer will own when a
version is cut and which one a caller gets. Keeping this pure is what makes the round
trip — the one thing PR-3 depends on — cheap to test exhaustively.

**The round trip is the whole point of this module.** ``resolve_agent_invocation`` takes an
``Assistant`` (``inference_api/chat/agent_binding_resolver.py``), so if a version
deserializes back into that shape then choosing *which* Assistant a turn runs is a single
well-defined swap and everything downstream — binding resolution, model access checks, the
harness — is untouched. Get this seam right and the invocation change is one line; get it
wrong and it becomes a rewrite.

Two properties hold, and both are asserted in ``tests/shared/test_agent_versions.py``:

1. **Snapshot → apply is the identity** on an Agent that has not been edited since.
   ``apply_version(agent, snapshot_of(agent)) == agent``, extras and all. That is what
   makes "run the published version" provably equivalent to today's behavior at the moment
   of approval, rather than a second code path that drifts.
2. **Apply is an overlay, never a replacement.** A version deliberately carries no
   ``ownerId``, ``visibility`` or ``status`` (see ``AgentVersion``), and carries no
   identity fields at all — ``assistantId``, ``vectorIndexId``, ``createdAt``, share
   metadata. Those come from the live record every time. So applying a version answers
   "what does this Agent *do*", and never "who may reach it" — which stays the live access
   decision it has always been.
"""

from typing import Optional, Tuple

from .models import AgentVersion, Assistant

# Child-item sort key for a version, under the Agent's own partition. Zero-padded to eight
# digits so the key sorts lexically — a plain ``VERSION#10`` would sort before
# ``VERSION#9``, and "the highest version" is read as the last key in the partition.
VERSION_SK_PREFIX = "VERSION#"
VERSION_NUMBER_WIDTH = 8

# The fields a version freezes, named once so the capture, the overlay and their test all
# agree on the list. Attribute names are the Python ones; the aliases are pydantic's job.
#
# ⚠️ Adding a field here changes what an already-approved version *fails* to carry — an
# older version item simply has no value for it, and the overlay must leave the live
# record's value in place rather than blanking it. That is why the overlay skips unset
# fields rather than writing every name in this tuple unconditionally.
SNAPSHOT_FIELDS: Tuple[str, ...] = (
    "name",
    "description",
    "instructions",
    "tagline",
    "emoji",
    "icon_key",
    "starters",
    "model_settings",
    "bindings",
)

# Snapshot fields that live on the ``listing`` block rather than on the Agent itself.
LISTING_SNAPSHOT_FIELDS: Tuple[str, ...] = ("category", "publisher_id")


# Fields the review diff reports on, in the order a reviewer reads them (§6.1). Behavior
# first, because that is what a review is *for*: a resubmission that only fixes a tagline
# should be approvable in seconds, and one that rewrites the instructions should be the
# first thing on the page.
#
# Derived from the snapshot lists rather than re-typed, so a field added to a version can
# never be silently absent from the diff — which would be the worst possible failure here:
# a reviewer told "nothing changed" about something that did.
DIFF_FIELD_ORDER: Tuple[str, ...] = (
    "instructions",
    "bindings",
    "model_settings",
    "name",
    "description",
    "tagline",
    "starters",
    "emoji",
    "icon_key",
    "category",
    "publisher_id",
)

assert set(DIFF_FIELD_ORDER) == set(SNAPSHOT_FIELDS) | set(LISTING_SNAPSHOT_FIELDS), (
    "DIFF_FIELD_ORDER must cover exactly the snapshot fields — a field missing here is a "
    "change a reviewer would never be shown."
)


def version_sk(number: int) -> str:
    """The DynamoDB sort key for version ``number``.

    Raises ``ValueError`` below 1: versions are 1-based, and a 0 would collide with the
    "not persisted yet" sentinel on ``AgentVersion.version``.
    """
    if number < 1:
        raise ValueError(f"Version numbers are 1-based; got {number}.")
    return f"{VERSION_SK_PREFIX}{number:0{VERSION_NUMBER_WIDTH}d}"


def version_number_from_sk(sort_key: str) -> Optional[int]:
    """Parse a version number back out of a sort key, or ``None`` if it is not one.

    Tolerant on purpose — it reads whatever the partition returns, and a sibling child row
    (a ``REPORT#…``) or a key written by newer code should be skipped, not raised over.
    """
    if not sort_key.startswith(VERSION_SK_PREFIX):
        return None
    try:
        return int(sort_key[len(VERSION_SK_PREFIX) :])
    except ValueError:
        return None


def snapshot_of(
    assistant: Assistant, *, created_at: Optional[str] = None, created_by: Optional[str] = None
) -> AgentVersion:
    """Capture ``assistant``'s reviewable surface as an unnumbered ``AgentVersion``.

    The result carries ``version=None``: the number is allocated by
    ``version_repository.create_version`` at write time, because only the conditional
    write can settle two concurrent submissions.

    ``category`` and ``publisherId`` come from the Agent's ``listing`` block and are
    ``None`` when it has none. That is the pre-submission case, and it is left honest
    rather than defaulted — a version with no category is a version that was never on a
    shelf, and inventing one would put it on the wrong one.
    """
    listing = assistant.listing
    return AgentVersion(
        agent_id=assistant.assistant_id,
        version=None,
        created_at=created_at,
        created_by=created_by,
        name=assistant.name,
        description=assistant.description,
        instructions=assistant.instructions,
        tagline=assistant.tagline,
        emoji=assistant.emoji,
        icon_key=assistant.icon_key,
        starters=assistant.starters,
        model_settings=assistant.model_settings,
        bindings=assistant.bindings,
        category=listing.category if listing else None,
        publisher_id=listing.publisher_id if listing else None,
    )


def apply_version(assistant: Assistant, version: AgentVersion) -> Assistant:
    """Overlay ``version``'s frozen surface onto ``assistant``, returning a new Assistant.

    The input is not mutated — callers hold the live record for the access decision that
    already happened, and quietly rewriting it under them is how a snapshot ends up
    deciding reachability.

    Only fields the version actually **set** are overlaid. An older version item that
    predates a snapshot field carries no value for it, and blanking the live record's value
    would be a silent behavior change on exactly the Agents that have been approved longest.

    ``category``/``publisherId`` are written back onto a copy of the listing block, so the
    store renders the placement the reviewer approved. ``listing.state`` and everything else
    on the block stay live: publication state is a fact about *now*, not something a
    snapshot gets a vote on. An Agent with no listing gets none synthesized — publication
    is an explicit forward act (``assistants.listing``), and a version is not one. A version
    cut before submission carries ``category=None``, and that never blanks a live category:
    a published listing with no category has no shelf to sit on and ``gsi5_keys`` refuses it.
    """
    set_fields = version.model_fields_set
    updated = assistant.model_copy(deep=True)

    for field in SNAPSHOT_FIELDS:
        if field in set_fields:
            setattr(updated, field, getattr(version, field))

    listing_updates = {
        field: getattr(version, field) for field in LISTING_SNAPSHOT_FIELDS if field in set_fields
    }
    if listing_updates and updated.listing is not None:
        listing = updated.listing
        for field, value in listing_updates.items():
            if value is not None:
                setattr(listing, field, value)

    return updated


def to_assistant(assistant: Assistant, version: Optional[AgentVersion]) -> Assistant:
    """``apply_version``, tolerating a missing version.

    The shape PR-3's call site wants: "give me the Assistant this caller should run", where
    ``None`` means there is no published version and the live record is the answer. Folding
    the null case in here keeps the fallback in one place rather than at every reader.
    """
    if version is None:
        return assistant
    return apply_version(assistant, version)
