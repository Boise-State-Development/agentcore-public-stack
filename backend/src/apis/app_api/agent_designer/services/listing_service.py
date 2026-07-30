"""Agent Marketplace Phase 1 — submit / review / takedown orchestration (D2, D7, D12, D13).

Where the authorization, the disclosure checks and the state machine meet. The machine
itself is pure (``apis.shared.assistants.listing``) and the writes are isolated
(``apis.shared.assistants.listing_repository``); this module decides *whether* a given
caller may walk a given edge, and what has to be true first.

Three rules are enforced here and nowhere else:

* **Submission is the disclosure point (D7).** Publishing an Agent effectively publishes
  the contents of every skill its author wrote and bound, because Skills v2 resolves a
  ``skill`` binding on ``skill.owner_id == agent.owner_id``. The author is shown that list
  before a reviewer's time is spent, and a ``memory_space`` binding blocks submission
  outright — a memory space is personal data that re-resolution will deny to every other
  viewer, so a published agent bound to one is a guaranteed failure for everyone.
* **Approval is the only door into the store (D2).** ``in_review → published`` is the sole
  edge that writes a directory key, and only ``require_admin`` routes reach it.
* **Admins own presentation, authors own behavior (D13).** The patch path can reach
  ``name``/``tagline``/``iconKey``/``category``/``publisherId`` and nothing else, and every
  such edit is recorded on the listing so the author is told rather than surprised.

⚠️ ``publisherId`` appears nowhere in an access decision in this file. Ownership
(``resolve_assistant_permission``) is what gates the author paths; ``require_admin`` gates
the rest. Publisher is a name on a shelf.
"""

import logging
import os
from typing import List, Optional, Set, Tuple

from apis.shared.assistants.compat import effective_bindings
from apis.shared.assistants.categories import ensure_seeded
from apis.shared.assistants.icons import icon_url
from apis.shared.assistants.listing import (
    PENDING_DECISION_STATES,
    ListingAuthorityError,
    ListingTransitionError,
    assert_author_target,
    assert_transition,
    gsi5_keys,
    is_listed,
)
from apis.shared.assistants.listing_repository import list_by_state, write_listing
from apis.shared.assistants.version_diff import (
    behavior_changed,
    changed_fields,
    instructions_diff,
)
from apis.shared.assistants.version_repository import (
    create_version,
    get_version,
    set_version_index,
)
from apis.shared.assistants.versions import snapshot_of
from apis.shared.assistants.models import (
    AdminEdit,
    AdminListingPatchRequest,
    AdminListingRow,
    AgentListing,
    AgentVersionDiffResponse,
    Assistant,
    VersionFieldChange,
    PublisherProfile,
    SkillExposure,
    SubmitListingRequest,
)
from apis.shared.assistants.publishers import (
    ensure_individual_profile,
    get_publisher,
    list_publishers,
    list_publishers_for_user,
)
from apis.shared.assistants.service import (
    _get_assistant_cloud_without_ownership_check,
    resolve_assistant_permission,
)
from apis.shared.auth.models import User
from apis.shared.feature_flags import skills_enabled
from apis.shared.memory.service import MemorySpaceService
from apis.shared.skills.repository import get_skill_catalog_repository
from apis.shared.timestamps import utc_now_iso

logger = logging.getLogger(__name__)


class ListingError(Exception):
    """A listing operation the caller may not perform, or that is not yet valid.

    ``status_code`` maps to the HTTP response: 403 for an authorization denial, 404 for a
    missing agent, 400 for a request the current state or bindings do not permit.
    """

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# The author reads these ("An admin updated the category on Jul 24"), so record the field
# by the name they'd recognize rather than the internal attribute.
_EDIT_FIELD_LABELS = {
    "icon_key": "icon",
    "publisher_id": "publisher",
}


# What the Review queue asks for: "everything needing a decision", spelled as a state so the
# route keeps one query parameter instead of growing a second one.
_PENDING_QUERY = "pending"


def _now() -> str:
    return utc_now_iso()


def _current_state(assistant: Assistant) -> Optional[str]:
    """The listing state, or ``None`` for a record that has never been submitted (D3)."""
    return assistant.listing.state if assistant.listing else None


async def _validate_category(category: str) -> None:
    """Check a category against the admin-managed set (D10, Phase 2).

    Phase 1 checked a constant; the records are the source now. ``ensure_seeded`` makes
    the first call in a fresh environment write the defaults rather than reject every
    submission, so an unseeded environment is never an unusable one.

    Disabled categories are refused for *new* submissions while listings already in them
    keep working — that is the whole point of disable-instead-of-delete.
    """
    categories = await ensure_seeded()
    match = next((c for c in categories if c.id == category), None)
    if match is None:
        available = ", ".join(c.id for c in categories if c.enabled)
        raise ListingError(
            f"Unknown category '{category}'. Expected one of: {available}.", status_code=400
        )
    if not match.enabled:
        raise ListingError(
            f"The category '{match.label}' is no longer accepting new listings.", status_code=400
        )


async def _load_for_author(agent_id: str, user: User) -> Assistant:
    """Load an Agent the caller owns, or raise.

    Submission and withdrawal are owner-only. An *editor* may change what an agent does,
    but putting the institution's name on it is the owner's act — and the owner is who
    D7's skill exposure actually concerns, since invoke-through resolves against
    ``agent.owner_id``.
    """
    assistant, permission = await resolve_assistant_permission(
        assistant_id=agent_id, user_id=user.user_id, user_email=user.email
    )
    if not assistant:
        raise ListingError(f"Agent not found: {agent_id}", status_code=404)
    if permission != "owner":
        raise ListingError(
            "Only the owner of an agent can manage its marketplace listing.", status_code=403
        )
    return assistant


async def _load_any(agent_id: str) -> Assistant:
    """Load an Agent without an ownership check — for admin paths (D13).

    ``update_assistant``/``get_assistant`` both gate on ``owner_id``, which a reviewer
    fails by definition; D13 exists so an admin can fix a tagline without the author.
    """
    table_name = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not table_name:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")
    assistant = await _get_assistant_cloud_without_ownership_check(agent_id, table_name)
    if not assistant:
        raise ListingError(f"Agent not found: {agent_id}", status_code=404)
    return assistant


# ── D7 disclosure ────────────────────────────────────────────────────────────────────
async def _memory_space_block_reason(assistant: Assistant, user: User) -> Optional[str]:
    """The D7.2 blocking message for a ``memory_space`` binding, or ``None`` if clear.

    Split from the raising path so ``preflight_listing`` can *show* the block without
    attempting the transition. One function decides, two callers present it — a second
    copy of this rule in the SPA would be the thing that eventually disagrees.
    """
    bound = [b for b in effective_bindings(assistant) if b.kind == "memory_space"]
    if not bound:
        return None

    # Resolve a human name for the message. The id alone tells the author nothing.
    label = bound[0].ref
    try:
        spaces = MemorySpaceService().list_spaces_for_user(user.user_id, user.email)
        by_id = {space.space_id: space.name for space, _role in spaces}
        label = by_id.get(bound[0].ref, bound[0].ref)
    except Exception:
        logger.warning("Could not resolve memory space name for submission block", exc_info=True)

    return (
        f"This agent can't be published while it's bound to the memory space “{label}”. "
        "A memory space is personal data — it won't resolve for anyone else, so the agent "
        "would fail for every person who ran it. Remove the binding and submit again."
    )


async def _memory_space_block(assistant: Assistant, user: User) -> None:
    """Block submission on any ``memory_space`` binding, naming the space (D7.2).

    Not a warning. A memory space is personal data; Designer D5's run-time re-resolve
    already denies it to anyone who lacks access, so publishing an agent bound to one
    ships a listing that cannot work for a single other person.
    """
    reason = await _memory_space_block_reason(assistant, user)
    if reason:
        raise ListingError(reason, status_code=400)


def _visibility_block_reason(assistant: Assistant) -> Optional[str]:
    """The blocking message for an Agent that is not ``PUBLIC``, or ``None`` if clear.

    **The marketplace is public-only.** Sharing an Agent with named coworkers is a separate
    mechanism with its own control on the agent tile, and a listing carries no audience of
    its own — ``AgentListing`` has no scope field, and the store is one global shelf. So a
    published SHARED or PRIVATE Agent is not a "team listing"; it is a tile shown to
    everyone that nobody but the author can open, and every pin against it 404s.

    Split from the raising path for the same reason as ``_memory_space_block_reason``:
    ``preflight_listing`` shows it, ``submit_listing`` enforces it, and one function
    decides so the dialog and the transition cannot drift apart.
    """
    if assistant.visibility == "PUBLIC":
        return None
    if assistant.visibility == "SHARED":
        return (
            "This agent can't be published while it's shared with specific people. The "
            "store is public — everyone would see it, but only the people it's shared "
            "with could open it. Set Visibility to Public to publish it, or keep sharing "
            "it directly instead."
        )
    return (
        "This agent can't be published while it's private. The store is public, so people "
        "would see it and get an error when they opened it. Set Visibility to Public and "
        "submit again."
    )


def _visibility_block(assistant: Assistant, *, consented: bool) -> None:
    """Refuse an Agent that is not ``PUBLIC`` and whose author has not consented to it.

    Not a silent widening — publication must never be a side door that changes who can
    reach an Agent. But the refusal alone made the *common* path a dead end: every Agent
    starts PRIVATE, so a first-time author was told to go set visibility on another screen
    and come back. ``consented`` is the submit dialog's checkbox: the author is looking at
    what the store will say about their Agent and ticks a box that says it becomes public.
    That is consent captured where it means something, and it is why the widening is
    allowed to ride the same write.

    An omitted flag still refuses, so a direct API caller cannot widen an Agent by accident.
    """
    if consented:
        return
    reason = _visibility_block_reason(assistant)
    if reason:
        raise ListingError(reason, status_code=400)


async def _exposed_skills(assistant: Assistant) -> List[SkillExposure]:
    """Skills the author wrote that publication makes readable (D7.1).

    Matches the invoke-through rule exactly: a ``skill`` binding resolves when
    ``skill.owner_id == agent.owner_id``, so those — and only those — are the skills whose
    contents publication exposes. Skills the author merely has access to belong to someone
    else and are not the author's to disclose.
    """
    refs = [b.ref for b in effective_bindings(assistant) if b.kind == "skill"]
    if not refs or not skills_enabled():
        return []
    try:
        skills = await get_skill_catalog_repository().batch_get_skills(refs)
    except Exception:
        logger.warning("Could not resolve skill names for submission disclosure", exc_info=True)
        return [SkillExposure(ref=r, label=r) for r in refs]

    return [
        SkillExposure(ref=s.skill_id, label=s.display_name)
        for s in skills
        if s.owner_id == assistant.owner_id
    ]


# ── publisher resolution (D12) ───────────────────────────────────────────────────────
async def _resolve_proposed_publisher(user: User, publisher_id: Optional[str]) -> str:
    """The publisher an author may propose, defaulting to their own individual profile.

    Eligibility is checked *here*, on the author's proposal path only. An admin may set
    any publisher on any listing regardless of it (D12) — see ``patch_listing_presentation``,
    which deliberately does not call this.
    """
    if not publisher_id:
        profile = await ensure_individual_profile(user.user_id, user.name)
        return profile.id

    profile = await get_publisher(publisher_id)
    if not profile or not profile.enabled:
        raise ListingError(f"Unknown publisher '{publisher_id}'.", status_code=400)

    eligible = await list_publishers_for_user(user.user_id)
    if publisher_id not in eligible:
        raise ListingError(
            f"You aren't eligible to publish as “{profile.label}”. "
            "An admin can grant that, or you can submit under your own name.",
            status_code=403,
        )
    return publisher_id


# ── author transitions ───────────────────────────────────────────────────────────────
async def preflight_listing(
    agent_id: str, user: User
) -> Tuple[List[SkillExposure], Optional[str], str, bool]:
    """Run the D7 checks **without** transitioning, for the submit dialog.

    D7.1 asks the dialog to enumerate the exposed skills *before* the author commits,
    and D7.2's block is more useful as a disabled button with a reason than as an error
    after the click. Both answers come from the same helpers ``submit_listing`` uses, so
    what the author is shown and what the transition enforces cannot drift apart.

    Owner-only, like every other author path: the skill exposure is a statement about
    what the *owner's* publication would reveal, and it is not an editor's to see.

    ``requires_public`` is deliberately **not** folded into ``block_reason``. A block sends
    the author out of the dialog; needing to go public is something the dialog itself can
    resolve, with the consent checkbox that sets ``make_public``. Returning them as one
    field is what made the ordinary path a dead end.

    Reachability still rides along for the same reason it always did — an Agent published
    as PUBLIC can be narrowed afterwards, which no submit-time gate can catch.
    """
    assistant = await _load_for_author(agent_id, user)
    reachability = _reachability(assistant)
    requires_public = _visibility_block_reason(assistant) is not None
    block_reason = await _memory_space_block_reason(assistant, user)
    # An agent that cannot be published at all is not first walked through a
    # skill-exposure confirmation.
    if block_reason:
        return [], block_reason, reachability, requires_public
    return await _exposed_skills(assistant), None, reachability, requires_public


async def submit_listing(
    agent_id: str, user: User, request: SubmitListingRequest
) -> Tuple[AgentListing, List[SkillExposure]]:
    """Author submits an Agent for review (D2), after the D7 checks pass."""
    assistant = await _load_for_author(agent_id, user)
    await _validate_category(request.category)

    try:
        assert_transition(_current_state(assistant), "in_review")
    except ListingTransitionError as e:
        raise ListingError(str(e), status_code=400) from e

    # Order matters: block before disclosing. An author whose agent cannot be published
    # at all should not first be walked through a skill-exposure confirmation.
    await _memory_space_block(assistant, user)
    _visibility_block(assistant, consented=request.make_public)
    exposed = await _exposed_skills(assistant)
    publisher_id = await _resolve_proposed_publisher(user, request.publisher_id)

    now = _now()
    previous = assistant.listing

    # ── the snapshot (version-snapshots §3.2) ────────────────────────────────────────
    # Cut **here**, at submission, rather than at approval. Taking it at approval leaves a
    # window: the author submits, the admin reads it, the author edits, the admin approves
    # — and what gets published is not what was read. That is the same class of bug this
    # whole feature exists to close, just narrower. Freezing now means the reviewer is
    # always looking at an artifact that cannot move under them, and the cost is that
    # changing a pending submission means withdrawing and resubmitting (which cuts a new
    # version rather than mutating the pending one).
    #
    # The proposed category, publisher and tagline are folded in first, so the snapshot is
    # the submission as the author composed it — not the record as it stood a moment before.
    tagline = (request.tagline or "").strip() or None
    proposed = assistant.model_copy(
        update={
            "tagline": tagline if tagline is not None else assistant.tagline,
            "listing": AgentListing(
                state="in_review", category=request.category, publisher_id=publisher_id
            ),
        }
    )
    version = await create_version(
        agent_id, snapshot_of(proposed, created_at=now, created_by=user.user_id)
    )

    listing = AgentListing(
        state="in_review",
        category=request.category,
        publisher_id=publisher_id,
        submitted_at=now,
        submitted_by=user.user_id,
        submitted_version=version.version,
        # A resubmission does not unpublish anything. The previously approved version keeps
        # its index key and keeps serving until an admin promotes the new one, so the shelf
        # never goes blank while a review is pending.
        published_version=previous.published_version if previous else None,
        # A resubmission carries its review history forward; the note stays visible until
        # the reviewer replaces it, so the author keeps the context they are acting on.
        reviewed_at=previous.reviewed_at if previous else None,
        reviewed_by=previous.reviewed_by if previous else None,
        review_note=request.note or (previous.review_note if previous else None),
        admin_edits=previous.admin_edits if previous else [],
    )
    # The tagline rides this write rather than a second one (same reason as the D13 patch
    # path). ``None`` means "leave it alone" — an author resubmitting without touching the
    # field must not have their existing subtitle blanked.
    #
    # Only widen when it actually needs widening: an Agent that is already PUBLIC must not
    # have ``visibility`` rewritten just because the box was ticked, and a no-op write is
    # a lie in the audit trail.
    widen_to = "PUBLIC" if (request.make_public and assistant.visibility != "PUBLIC") else None
    await write_listing(
        agent_id,
        listing,
        assistant.created_at,
        updated_at=now,
        tagline=tagline,
        visibility=widen_to,
    )
    if widen_to:
        logger.info(
            f"🌐 Agent {agent_id} widened {assistant.visibility} → PUBLIC on submission "
            f"by {user.user_id}"
        )
    logger.info(f"📨 Agent {agent_id} submitted for review by {user.user_id}")
    return listing, exposed


async def withdraw_listing(agent_id: str, user: User) -> AgentListing:
    """Author withdraws — immediately if nothing is live, as a *request* if something is.

    **The same endpoint, two different acts, and the listing state decides which.** Before
    publication, withdrawing is the author's alone: a pending submission is their own work
    and pulling it costs nobody anything. Once a listing is live, other people have pinned
    it, so removal becomes something an admin sees — the same reasoning that makes
    publication go through a queue in the first place (D2). Splitting these across two
    endpoints was the alternative and is worse: the author's intent is identical either way
    ("take this down"), and making them pick the right verb for their listing's state is
    asking them to know the state machine.

    Neither act revokes anything retroactively: people who pinned it keep their pin,
    conversations underway keep running, and the agent stays reachable by direct link
    because ``visibility`` is a separate axis. It is a delisting, not a recall.
    """
    assistant = await _load_for_author(agent_id, user)
    if not assistant.listing:
        raise ListingError("This agent has no marketplace listing.", status_code=404)

    # A live listing can only be *requested* down; anything else goes straight to private.
    #
    # ⚠️ Known gap, left deliberately rather than patched here: a listing that was published
    # and then sent back for changes is still serving (``review_listing`` does not
    # unpublish), but its state is ``changes_requested``, so this reads it as not-live and
    # the author goes straight to ``private`` — pulling something users can currently see
    # without an admin deciding. Closing it needs a product call, not a predicate swap: the
    # transition table cannot allow ``changes_requested → withdrawal_requested`` without
    # also opening a route to ``published`` for a listing that was never approved (see
    # ``ALLOWED_TRANSITIONS``), and it is unclear what declining such a request should
    # restore the listing to.
    target = "withdrawal_requested" if is_listed(assistant.listing.state) else "private"

    try:
        assert_transition(assistant.listing.state, target)
        assert_author_target(target)
    except ListingTransitionError as e:
        raise ListingError(str(e), status_code=400) from e
    except ListingAuthorityError as e:  # pragma: no cover - both targets are author states
        raise ListingError(str(e), status_code=403) from e

    now = _now()
    if target == "withdrawal_requested":
        # ⚠️ The index is deliberately NOT cleared and ``publishedVersion`` deliberately
        # kept. The listing stays live while the request is pending — an author whose
        # request took it off the shelf immediately would have unilaterally unpublished it,
        # which is exactly what this state exists to prevent. A declined request then needs
        # no repair, because nothing was undone.
        listing = assistant.listing.model_copy(
            update={"state": target, "withdrawal_requested_at": now}
        )
        await write_listing(agent_id, listing, assistant.created_at, updated_at=now)
        logger.info(f"🙋 Agent {agent_id} withdrawal requested by its owner {user.user_id}")
        return listing

    # Pre-publication withdrawal: nothing is on the shelf, so this is immediate. The
    # unindex is belt-and-braces for a listing whose pointer outlived its key.
    await _unindex_version(agent_id, assistant.listing.published_version)
    listing = assistant.listing.model_copy(update={"state": target, "published_version": None})
    await write_listing(agent_id, listing, assistant.created_at, updated_at=now)
    logger.info(f"📭 Agent {agent_id} submission withdrawn by its owner")
    return listing


async def decide_withdrawal(
    agent_id: str, admin: User, *, decision: str, note: Optional[str] = None
) -> AgentListing:
    """Admin grants or declines an author's withdrawal request (§5.1).

    ``grant`` takes the listing to ``private`` and off the shelf. ``decline`` returns it to
    ``published`` and changes nothing else — the listing never stopped being live, so there
    is no key to restore and no version to re-promote. That asymmetry is the payoff of
    leaving the index alone while the request was pending.

    A declining admin should say why, since the author asked for something and is not
    getting it; ``note`` renders on their card exactly as a request-changes reason does.
    """
    assistant = await _load_any(agent_id)
    if not assistant.listing:
        raise ListingError("This agent has no marketplace listing.", status_code=404)
    if assistant.listing.state != "withdrawal_requested":
        raise ListingError(
            "This agent has no pending withdrawal request.",
            status_code=400,
        )

    target = "private" if decision == "grant" else "published"
    try:
        assert_transition(assistant.listing.state, target)
    except ListingTransitionError as e:
        raise ListingError(str(e), status_code=400) from e

    now = _now()
    changes: dict = {
        "state": target,
        "reviewed_at": now,
        "reviewed_by": admin.user_id,
        "review_note": note or None,
    }
    if target == "private":
        changes["published_version"] = None
        # Key first, record second — the fail-closed ordering in ``_unindex_version``.
        await _unindex_version(agent_id, assistant.listing.published_version)

    listing = assistant.listing.model_copy(update=changes)
    await write_listing(agent_id, listing, assistant.created_at, updated_at=now)
    logger.info(f"🙋 Agent {agent_id} withdrawal {decision}ed by {admin.user_id}")
    return listing


# ── version promotion ────────────────────────────────────────────────────────────────
async def _publish_version(
    agent_id: str,
    number: int,
    *,
    category: str,
    agent_created_at: str,
    superseding: Optional[int] = None,
) -> None:
    """Point the store at version ``number``, taking the key off whatever it replaces.

    Order matters and is the opposite of what feels natural: **write the new key first,
    then clear the old.** Clearing first would leave the shelf blank for the width of a
    round trip, and a blank shelf is a worse failure than a momentary duplicate — one is a
    published Agent vanishing, the other is the same Agent appearing under two versions
    until the second call lands.

    ``agent_created_at`` is the sort key, deliberately the *Agent's* creation timestamp and
    not the version's: browse is newest-first by Agent, and keying on version age would let
    a resubmission of a two-year-old Agent jump the top of the shelf every time it was
    re-approved. Promotion is not publication of a new thing.

    ``category`` comes from the **listing**, not from the snapshot. Placement is the one
    thing about a published version that an admin may legitimately change afterwards (D13),
    and it is expressed as the index key rather than written into the frozen record — which
    is exactly the line this design draws everywhere: content is immutable, *where it sits*
    is a fact about now. ``version.category`` stays as the author proposed and the reviewer
    saw it; the shelf a row appears on is the key.
    """
    await set_version_index(
        agent_id, number, gsi5_keys("published", category, agent_created_at)
    )
    if superseding is not None and superseding != number:
        await _unindex_version(agent_id, superseding)


async def _unindex_version(agent_id: str, number: Optional[int]) -> None:
    """Take a version off the shelf, tolerating one that is already gone.

    ⚠️ **Call this BEFORE writing the listing, and write the listing before calling
    ``_publish_version``.** The store index and the listing block used to be one
    ``update_item`` — ``listing_repository`` says so, and that atomicity is what made "an
    unpublished agent cannot be in the store" a fact rather than a hope. Moving the index
    onto the version row split it into two writes on two items, and the invariant now has to
    be bought with **ordering** instead:

        publish   → write the listing, then write the key   (partial ⇒ recorded, not shelved)
        unpublish → clear the key, then write the listing   (partial ⇒ not shelved, recorded live)

    Both partial outcomes leave the Agent **off** the shelf. The reverse orders leave it on
    the shelf while the record says otherwise, which is the single failure the sparse index
    exists to prevent. A DynamoDB transaction would restore true atomicity and is the honest
    upgrade if this ever needs to be stronger than fail-closed.

    A missing version row here is not worth failing a takedown over: the outcome the caller
    wants — "this is not in the store" — is already true, and raising would leave an admin
    unable to complete a delisting because of a row that does not exist.
    """
    if number is None:
        return
    try:
        await set_version_index(agent_id, number, None)
    except ValueError:
        logger.warning(f"Version {number} of {agent_id} is already absent; nothing to unindex")


# ── admin transitions ────────────────────────────────────────────────────────────────
async def review_listing(
    agent_id: str,
    admin: User,
    *,
    decision: str,
    note: Optional[str] = None,
    category: Optional[str] = None,
    publisher_id: Optional[str] = None,
) -> AgentListing:
    """Approve a submission, or return it with a reason (D2).

    Approval is where an attribution becomes authoritative, so the reviewer may adjust
    category and publisher in the same act (D12) without a second round trip.
    """
    assistant = await _load_any(agent_id)
    if not assistant.listing:
        raise ListingError("This agent has no marketplace listing to review.", status_code=404)

    target = "published" if decision == "approve" else "changes_requested"
    if decision == "request_changes" and not (note or "").strip():
        raise ListingError(
            "Requesting changes needs a reason — it renders on the author's card so they "
            "never have to ask what happened.",
            status_code=400,
        )

    try:
        assert_transition(assistant.listing.state, target)
    except ListingTransitionError as e:
        raise ListingError(str(e), status_code=400) from e

    # Re-checked here, not just at submit: ``visibility`` is an independent axis the author
    # can narrow at any point after submitting, so the gate that ran then says nothing about
    # now. Approving anyway would shelve a tile that 404s for every person who taps it.
    # Reviewer-facing wording — it is not this admin's job to widen someone else's access.
    if target == "published" and assistant.visibility != "PUBLIC":
        raise ListingError(
            f"This agent's visibility is now {assistant.visibility.title()}, so it can't be "
            "published — the store is public, and everyone but the author would get an "
            "error opening it. Request changes and ask the author to set it to Public.",
            status_code=400,
        )

    if category is not None:
        await _validate_category(category)
    if publisher_id is not None:
        # No eligibility check: an admin may attribute any listing to any publisher (D12).
        # That is how the store gets its day-one set of official Agents without those
        # Agents carrying a staff member's personal name.
        if not await get_publisher(publisher_id):
            raise ListingError(f"Unknown publisher '{publisher_id}'.", status_code=400)

    now = _now()
    changes = {
        "state": target,
        "category": category or assistant.listing.category,
        "publisher_id": publisher_id or assistant.listing.publisher_id,
        "reviewed_at": now,
        "reviewed_by": admin.user_id,
        "review_note": note or None,
    }
    # Approval promotes the version cut at submission — the artifact this admin actually
    # read — never "the latest", which an admin presentation edit (§6.2) could have moved
    # underneath them.
    #
    # ⚠️ A submission with no version predates this feature. Refusing is the safe answer:
    # publishing it would put an unversioned listing on a shelf that reads versions, which
    # renders as an empty tile. The author resubmits and gets one.
    promoted: Optional[int] = None
    if target == "published":
        promoted = assistant.listing.submitted_version
        if promoted is None:
            raise ListingError(
                "This submission predates version snapshots and has nothing to publish. "
                "Ask the author to resubmit — it will be captured on the way in.",
                status_code=400,
            )
        changes["published_version"] = promoted

    listing = assistant.listing.model_copy(update=changes)
    await write_listing(agent_id, listing, assistant.created_at, updated_at=now)

    # Index last, and only after the listing write succeeded. The index is what the store
    # actually answers from, so a key written against a listing that failed to persist
    # would shelve something no record claims is published.
    if promoted is not None:
        await _publish_version(
            agent_id,
            promoted,
            category=listing.category,
            agent_created_at=assistant.created_at,
            superseding=assistant.listing.published_version,
        )

    logger.info(f"⚖️ Agent {agent_id} review by {admin.user_id}: {decision} → {target}")
    return listing


async def takedown_listing(agent_id: str, admin: User, reason: str) -> AgentListing:
    """Delist a published Agent, clearing its directory key (D2).

    A **delisting, not a revocation**: existing pins keep working, conversations underway
    keep running, and the Agent stays reachable by direct link because ``visibility`` is
    the separate access axis. All this changes is whether the store can find it.
    """
    assistant = await _load_any(agent_id)
    if not assistant.listing:
        raise ListingError("This agent has no marketplace listing.", status_code=404)

    try:
        assert_transition(assistant.listing.state, "taken_down")
    except ListingTransitionError as e:
        raise ListingError(str(e), status_code=400) from e

    now = _now()
    # Off the shelf before the record says so — a takedown that half-failed must leave the
    # Agent invisible, never visible-but-recorded-down.
    await _unindex_version(agent_id, assistant.listing.published_version)
    listing = assistant.listing.model_copy(
        update={
            "state": "taken_down",
            "reviewed_at": now,
            "reviewed_by": admin.user_id,
            "review_note": reason,
            # The pointer clears with the key. A taken-down listing that still named a
            # published version would read as "this is live" to every reader that trusts
            # the pointer, and PR-3 makes invocation one of those readers.
            "published_version": None,
        }
    )
    await write_listing(agent_id, listing, assistant.created_at, updated_at=now)
    logger.info(f"🚫 Agent {agent_id} taken down by {admin.user_id}")
    return listing


async def patch_listing_presentation(
    agent_id: str, admin: User, patch: AdminListingPatchRequest
) -> AgentListing:
    """Edit the presentation fields of a listing, recording each change (D13).

    Everything the store renders is admin-editable without the author's involvement; an
    admin fixing a typo or swapping an off-brand icon should not need a round trip. What
    an admin cannot touch is behavior — ``AdminListingPatchRequest`` refuses those fields
    at the model boundary, so by the time we are here the request is presentation-only.
    """
    assistant = await _load_any(agent_id)
    if not assistant.listing:
        raise ListingError("This agent has no marketplace listing.", status_code=404)

    changes = patch.model_dump(exclude_none=True)
    if not changes:
        raise ListingError("No presentation fields supplied.", status_code=400)
    if "category" in changes:
        await _validate_category(changes["category"])
    if "publisher_id" in changes and not await get_publisher(changes["publisher_id"]):
        raise ListingError(f"Unknown publisher '{changes['publisher_id']}'.", status_code=400)

    now = _now()
    edits = list(assistant.listing.admin_edits) + [
        AdminEdit(field=_EDIT_FIELD_LABELS.get(field, field), at=now, by=admin.name or admin.user_id)
        for field in sorted(changes)
    ]
    category = changes.get("category", assistant.listing.category)
    publisher_id = changes.get("publisher_id", assistant.listing.publisher_id)

    # ── an admin edit to a live listing cuts a version (§6.2) ────────────────────────
    # The store renders from the published snapshot now, so a tagline fix that only touched
    # the Agent row would land nowhere — D13 would look like it silently stopped working.
    # Of the two honest options the spec puts up, this is the one that keeps immutability
    # absolute: the version is the unit of "what an admin blessed", and an admin editing it
    # is still an admin blessing it. The cost is that a category fix reads as a release in
    # the version history, which is the cheaper wrong.
    #
    # Attributed to the admin, not the author — ``createdBy`` is audit, never authorization,
    # and mislabelling this would put the author's name on someone else's edit.
    was_listed = is_listed(assistant.listing.state)
    previously_published = assistant.listing.published_version
    promoted: Optional[int] = None
    if was_listed:
        edited = assistant.model_copy(
            update={
                "name": changes.get("name", assistant.name),
                "tagline": changes.get("tagline", assistant.tagline),
                "icon_key": changes.get("icon_key", assistant.icon_key),
                "listing": assistant.listing.model_copy(
                    update={"category": category, "publisher_id": publisher_id}
                ),
            }
        )
        version = await create_version(
            agent_id, snapshot_of(edited, created_at=now, created_by=admin.user_id)
        )
        promoted = version.version

    listing = assistant.listing.model_copy(
        update={
            "category": category,
            "publisher_id": publisher_id,
            "admin_edits": edits,
            **({"published_version": promoted} if promoted is not None else {}),
        }
    )
    # ``name``/``tagline``/``iconKey`` live on the Agent record, not the listing block, so
    # they ride the same single write rather than racing a second update.
    await write_listing(
        agent_id,
        listing,
        assistant.created_at,
        tagline=changes.get("tagline"),
        icon_key=changes.get("icon_key"),
        name=changes.get("name"),
        updated_at=now,
    )
    if promoted is not None:
        await _publish_version(
            agent_id,
            promoted,
            category=category,
            agent_created_at=assistant.created_at,
            superseding=previously_published,
        )
    logger.info(f"✏️ Admin {admin.user_id} edited listing presentation for {agent_id}: {sorted(changes)}")
    return listing


# ── admin reads ──────────────────────────────────────────────────────────────────────
# camelCase for the wire, so the SPA reads the same field names it already knows from
# ``AgentResponse``. Snake_case would leak the storage attribute names into the UI.
_DIFF_FIELD_ALIASES = {
    "model_settings": "modelConfig",
    "icon_key": "iconKey",
    "publisher_id": "publisherId",
}


def _wire_value(value):
    """Serialize a snapshot value for the diff payload, keeping ``None`` distinct from ``[]``."""
    if isinstance(value, list):
        return [_wire_value(item) for item in value]
    dump = getattr(value, "model_dump", None)
    return dump(by_alias=True) if dump else value


async def diff_pending_version(agent_id: str) -> AgentVersionDiffResponse:
    """What the pending submission changes against what is published (§6.1).

    Reads the two snapshots the listing points at — ``submittedVersion`` and
    ``publishedVersion`` — rather than "the latest two". An admin presentation edit (§6.2)
    cuts a version too, so ordinal arithmetic would sooner or later diff the wrong pair.

    Raises ``ListingError`` when there is nothing under review: a diff is a thing you read
    *before deciding*, and offering one for a listing with no pending submission would
    invite deciding on it.
    """
    assistant = await _load_any(agent_id)
    listing = assistant.listing
    if not listing:
        raise ListingError("This agent has no marketplace listing.", status_code=404)

    pending_number = listing.submitted_version
    if pending_number is None or listing.state not in PENDING_DECISION_STATES:
        raise ListingError(
            "This agent has nothing awaiting review, so there is no diff to show.",
            status_code=400,
        )

    pending = await get_version(agent_id, pending_number)
    if pending is None:
        raise ListingError(
            f"Version {pending_number} of this agent could not be loaded.", status_code=404
        )

    published_number = listing.published_version
    published = (
        await get_version(agent_id, published_number) if published_number is not None else None
    )
    # A pointer to a version that is gone is not the same as never having published: say so
    # by falling back to the first-submission rendering rather than diffing against nothing
    # and reporting every field as changed.
    first_submission = published is None

    changes = [
        VersionFieldChange(
            field=_DIFF_FIELD_ALIASES.get(field, field),
            before=_wire_value(before),
            after=_wire_value(after),
            behavior=field in ("instructions", "bindings", "model_settings"),
        )
        for field, before, after in changed_fields(published, pending)
    ]

    return AgentVersionDiffResponse(
        agent_id=agent_id,
        published_version=published.version if published else None,
        pending_version=pending_number,
        first_submission=first_submission,
        behavior_changed=behavior_changed(published, pending),
        changes=changes,
        instructions_diff=instructions_diff(published, pending),
    )



async def list_admin_listings(state: Optional[str] = None) -> Tuple[List[AdminListingRow], int]:
    """Rows for the Review queue / Listings tables, plus the pending-decision count.

    The count badges the admin nav so the queue is visible rather than discovered — the
    operational half of D2's answer to "a review queue makes publication stop".

    ``state`` accepts the pseudo-value ``"pending"``, which is what the Review queue asks
    for: both submissions and withdrawal requests (``PENDING_DECISION_STATES``). §5.1 is
    explicit that withdrawal requests belong in the *existing* queue — "one queue rather
    than a second surface to remember" — and a queue an admin has to remember to check is
    a queue that grows.
    """
    wanted: Optional[Set[str]] = None
    if state == _PENDING_QUERY:
        wanted = set(PENDING_DECISION_STATES)
    elif state is not None:
        wanted = {state}

    # A multi-state ask scans once and filters here rather than issuing one scan per state.
    # The population is every Agent anyone has ever submitted and the caller is a human
    # clicking a nav item, so one pass is cheaper than two round trips.
    raw = await list_by_state(state if wanted and len(wanted) == 1 else None)
    publishers = {p.id: p for p in await list_publishers()}

    rows: List[AdminListingRow] = []
    for item in raw:
        try:
            assistant = Assistant.model_validate(item)
        except Exception:
            logger.warning(f"Skipping unparseable assistant row {item.get('PK')}", exc_info=True)
            continue
        if not assistant.listing:
            continue
        if wanted is not None and assistant.listing.state not in wanted:
            continue
        rows.append(_to_row(assistant, publishers.get(assistant.listing.publisher_id)))

    rows.sort(key=lambda r: (r.submitted_at or r.updated_at), reverse=True)

    # The badge counts the whole decision queue, not whatever slice the caller asked for —
    # an admin filtering the Listings table to "published" still needs to see work waiting.
    # When the fetched rows already cover the queue, count them instead of re-scanning.
    if wanted is None or wanted >= set(PENDING_DECISION_STATES):
        pending = len([r for r in rows if r.state in PENDING_DECISION_STATES])
    else:
        pending = len(
            [
                item
                for item in await list_by_state(None)
                if (item.get("listing") or {}).get("state") in PENDING_DECISION_STATES
            ]
        )

    return rows, pending


# ``_instructions_hash`` and ``_drift`` lived here (#744). Both are gone rather than
# dormant: they detected an author editing a published Agent, and a published Agent is now
# an immutable snapshot the author cannot reach. The reviewer's real question — "is what I
# approved still what is live?" — is answered by ``listing.publishedVersion`` instead, which
# is a fact rather than a heuristic, and never had the weak ``edited`` fallback's habit of
# reporting an admin's own typo fix as a behavior change.


def _reachability(assistant: Assistant) -> str:
    """Who can actually open this Agent, projected from ``visibility`` (see ``ListingReachability``).

    Derived on every read rather than stored — ``visibility`` can change at any time and a
    cached copy would be wrong exactly when it mattered.

    ⚠️ This used to say publishing a SHARED Agent to a team was legitimate, and that it was
    the *only* thing standing between "approved" and a tile that 404s for everyone but the
    author. Both were wrong. The marketplace is public-only — sharing with named coworkers
    is a separate mechanism, and a listing has no audience of its own — so anything short
    of PUBLIC is now refused outright at submit and again at approve
    (``_visibility_block_reason``).

    What survives is the case no gate can catch: an Agent published as PUBLIC and narrowed
    afterwards. This is what tells a reviewer, and the admin listings table, that an
    already-published row has gone unreachable.
    """
    if assistant.visibility == "PUBLIC":
        return "everyone"
    if assistant.visibility == "SHARED":
        return "shared_only"
    return "owner_only"


def _to_row(assistant: Assistant, publisher: Optional[PublisherProfile]) -> AdminListingRow:
    listing = assistant.listing
    if listing is None:  # callers filter; belt-and-braces rather than a stripped assert
        raise ValueError(f"Agent {assistant.assistant_id} has no listing to project")
    return AdminListingRow(
        agent_id=assistant.assistant_id,
        name=assistant.name,
        tagline=assistant.tagline,
        emoji=assistant.emoji,
        icon_key=assistant.icon_key,
        icon_url=icon_url(assistant.assistant_id, assistant.icon_key),
        owner_name=assistant.owner_name,
        publisher=publisher,
        category=listing.category,
        state=listing.state,
        usage_count=assistant.usage_count,
        submitted_at=listing.submitted_at,
        # Only while one is actually pending: the stamp survives the decision on the stored
        # listing, and a resolved request rendering as "withdrawal requested 3 days ago"
        # would put a decided listing back in front of an admin as if it still needed one.
        withdrawal_requested_at=(
            listing.withdrawal_requested_at if listing.state == "withdrawal_requested" else None
        ),
        reviewed_at=listing.reviewed_at,
        review_note=listing.review_note,
        updated_at=assistant.updated_at,
        published_version=listing.published_version,
        reachability=_reachability(assistant),
        admin_edits=listing.admin_edits,
    )
