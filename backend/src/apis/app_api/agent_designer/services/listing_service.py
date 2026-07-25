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
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from apis.shared.assistants.compat import effective_bindings
from apis.shared.assistants.listing import (
    DEFAULT_CATEGORIES,
    ListingTransitionError,
    assert_transition,
)
from apis.shared.assistants.listing_repository import list_by_state, write_listing
from apis.shared.assistants.models import (
    AdminEdit,
    AdminListingPatchRequest,
    AdminListingRow,
    AgentListing,
    Assistant,
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat() + "Z"


def _current_state(assistant: Assistant) -> Optional[str]:
    """The listing state, or ``None`` for a record that has never been submitted (D3)."""
    return assistant.listing.state if assistant.listing else None


def _validate_category(category: str) -> None:
    if category not in DEFAULT_CATEGORIES:
        raise ListingError(
            f"Unknown category '{category}'. Expected one of: {', '.join(DEFAULT_CATEGORIES)}.",
            status_code=400,
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
async def _memory_space_block(assistant: Assistant, user: User) -> None:
    """Block submission on any ``memory_space`` binding, naming the space (D7.2).

    Not a warning. A memory space is personal data; Designer D5's run-time re-resolve
    already denies it to anyone who lacks access, so publishing an agent bound to one
    ships a listing that cannot work for a single other person.
    """
    bound = [b for b in effective_bindings(assistant) if b.kind == "memory_space"]
    if not bound:
        return

    # Resolve a human name for the message. The id alone tells the author nothing.
    label = bound[0].ref
    try:
        spaces = MemorySpaceService().list_spaces_for_user(user.user_id, user.email)
        by_id = {space.space_id: space.name for space, _role in spaces}
        label = by_id.get(bound[0].ref, bound[0].ref)
    except Exception:
        logger.warning("Could not resolve memory space name for submission block", exc_info=True)

    raise ListingError(
        f"This agent can't be published while it's bound to the memory space “{label}”. "
        "A memory space is personal data — it won't resolve for anyone else, so the agent "
        "would fail for every person who ran it. Remove the binding and submit again.",
        status_code=400,
    )


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
async def submit_listing(
    agent_id: str, user: User, request: SubmitListingRequest
) -> Tuple[AgentListing, List[SkillExposure]]:
    """Author submits an Agent for review (D2), after the D7 checks pass."""
    assistant = await _load_for_author(agent_id, user)
    _validate_category(request.category)

    try:
        assert_transition(_current_state(assistant), "in_review")
    except ListingTransitionError as e:
        raise ListingError(str(e), status_code=400) from e

    # Order matters: block before disclosing. An author whose agent cannot be published
    # at all should not first be walked through a skill-exposure confirmation.
    await _memory_space_block(assistant, user)
    exposed = await _exposed_skills(assistant)
    publisher_id = await _resolve_proposed_publisher(user, request.publisher_id)

    now = _now()
    previous = assistant.listing
    listing = AgentListing(
        state="in_review",
        category=request.category,
        publisher_id=publisher_id,
        submitted_at=now,
        submitted_by=user.user_id,
        # A resubmission carries its review history forward; the note stays visible until
        # the reviewer replaces it, so the author keeps the context they are acting on.
        reviewed_at=previous.reviewed_at if previous else None,
        reviewed_by=previous.reviewed_by if previous else None,
        review_note=request.note or (previous.review_note if previous else None),
        admin_edits=previous.admin_edits if previous else [],
    )
    await write_listing(agent_id, listing, assistant.created_at, updated_at=now)
    logger.info(f"📨 Agent {agent_id} submitted for review by {user.user_id}")
    return listing, exposed


async def withdraw_listing(agent_id: str, user: User) -> AgentListing:
    """Author unpublishes or withdraws a submission — back to ``private`` (D2).

    Unpublishing revokes nothing retroactively: people who pinned it keep their pin,
    conversations underway keep running, and the agent stays reachable by direct link
    because ``visibility`` is a separate axis. It is a delisting, not a recall.
    """
    assistant = await _load_for_author(agent_id, user)
    if not assistant.listing:
        raise ListingError("This agent has no marketplace listing.", status_code=404)

    try:
        assert_transition(assistant.listing.state, "private")
    except ListingTransitionError as e:
        raise ListingError(str(e), status_code=400) from e

    now = _now()
    listing = assistant.listing.model_copy(update={"state": "private"})
    await write_listing(agent_id, listing, assistant.created_at, updated_at=now)
    logger.info(f"📭 Agent {agent_id} unpublished by its owner")
    return listing


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

    if category is not None:
        _validate_category(category)
    if publisher_id is not None:
        # No eligibility check: an admin may attribute any listing to any publisher (D12).
        # That is how the store gets its day-one set of official Agents without those
        # Agents carrying a staff member's personal name.
        if not await get_publisher(publisher_id):
            raise ListingError(f"Unknown publisher '{publisher_id}'.", status_code=400)

    now = _now()
    listing = assistant.listing.model_copy(
        update={
            "state": target,
            "category": category or assistant.listing.category,
            "publisher_id": publisher_id or assistant.listing.publisher_id,
            "reviewed_at": now,
            "reviewed_by": admin.user_id,
            "review_note": note or None,
        }
    )
    await write_listing(agent_id, listing, assistant.created_at, updated_at=now)
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
    listing = assistant.listing.model_copy(
        update={
            "state": "taken_down",
            "reviewed_at": now,
            "reviewed_by": admin.user_id,
            "review_note": reason,
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
        _validate_category(changes["category"])
    if "publisher_id" in changes and not await get_publisher(changes["publisher_id"]):
        raise ListingError(f"Unknown publisher '{changes['publisher_id']}'.", status_code=400)

    now = _now()
    edits = list(assistant.listing.admin_edits) + [
        AdminEdit(field=_EDIT_FIELD_LABELS.get(field, field), at=now, by=admin.name or admin.user_id)
        for field in sorted(changes)
    ]
    listing = assistant.listing.model_copy(
        update={
            "category": changes.get("category", assistant.listing.category),
            "publisher_id": changes.get("publisher_id", assistant.listing.publisher_id),
            "admin_edits": edits,
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
    logger.info(f"✏️ Admin {admin.user_id} edited listing presentation for {agent_id}: {sorted(changes)}")
    return listing


# ── admin reads ──────────────────────────────────────────────────────────────────────
async def list_admin_listings(state: Optional[str] = None) -> Tuple[List[AdminListingRow], int]:
    """Rows for the Review queue / Listings tables, plus the pending-review count.

    The count badges the admin nav so the queue is visible rather than discovered — the
    operational half of D2's answer to "a review queue makes publication stop".
    """
    raw = await list_by_state(state)
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
        rows.append(_to_row(assistant, publishers.get(assistant.listing.publisher_id)))

    rows.sort(key=lambda r: (r.submitted_at or r.updated_at), reverse=True)

    # The badge counts the review queue, not whatever slice the caller asked for — an
    # admin filtering the Listings table to "published" still needs to see work waiting.
    # When the fetched rows already cover the queue, count them instead of re-scanning.
    if state is None or state == "in_review":
        pending = len([r for r in rows if r.state == "in_review"])
    else:
        pending = len(await list_by_state("in_review"))

    return rows, pending


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
        owner_name=assistant.owner_name,
        publisher=publisher,
        category=listing.category,
        state=listing.state,
        usage_count=assistant.usage_count,
        submitted_at=listing.submitted_at,
        reviewed_at=listing.reviewed_at,
        review_note=listing.review_note,
        updated_at=assistant.updated_at,
        admin_edits=listing.admin_edits,
    )
