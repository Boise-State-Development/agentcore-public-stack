"""Agent Marketplace Phase 1 — the listing state machine and the sparse directory key.

Pure functions over the ``listing`` block on an Agent record. No I/O: the repository
layer (``listing_repository``) turns a validated target state into a DynamoDB write, and
the service layer (``agent_designer.services.listing_service``) owns the authorization
and disclosure rules. Keeping the machine pure is what makes every transition — legal
and illegal — cheap to test.

Two invariants live here, and both are load-bearing:

1. **Publication is an explicit forward act** (spec D3). A record with no ``listing``
   block has never been submitted, and no amount of ``visibility == "PUBLIC"`` creates
   one. ``visibility`` is the access gate; ``listing.state`` is the publication state.
   They are separate axes and this module never reads the former.
2. **The directory index is written only while published** (spec Data model). ``gsi5_keys``
   returns keys for exactly one state, so unpublication is enforced by physics rather
   than by a filter someone can forget: no key, so the browse query cannot return it.
"""

from typing import Dict, Optional, Set, Tuple

# ── Categories (spec D10) ────────────────────────────────────────────────────────────
# Phase 1 validates ``listing.category`` against this constant set. Phase 2 replaces the
# source with admin-managed ``AGENT_CATEGORIES`` records (the ``UserMenuLink`` precedent:
# a fixed partition, per-item records with an explicit ``order``). The stored shape does
# NOT change — ``listing.category`` is a category id string either way — so that swap is
# a source change with no data migration.
#
# D10 is explicit that categories must not stay a build-time constant ("a category set
# that requires a deploy to change will not be maintained"). This is a one-phase
# expedient while nothing is user-visible, not the end state.
DEFAULT_CATEGORIES: Tuple[str, ...] = (
    "Administration",
    "Teaching",
    "Research",
    "Student Support",
    "IT & Operations",
    "Communications",
)

# ── States (spec D2) ─────────────────────────────────────────────────────────────────
LISTING_STATES: Tuple[str, ...] = (
    "private",
    "in_review",
    "published",
    "changes_requested",
    "taken_down",
)

# The transition table. ``None`` is the pre-state of a record that has never been
# submitted (no ``listing`` block at all) — the D3 backfill default.
#
# Each edge and the actor that walks it:
#
#   None              → in_review          author submits for the first time
#   private           → in_review          author submits
#   changes_requested → in_review          author resubmits after addressing the note
#   taken_down        → in_review          author resubmits after addressing the takedown
#                                          (the takedown dialog promises exactly this)
#   in_review         → published          admin approves
#   in_review         → changes_requested  admin requests changes, with a reason
#   published         → taken_down         admin delists, with a reason
#   published         → changes_requested  admin requests changes on a live listing
#   taken_down        → changes_requested  admin annotates an already-delisted listing
#   in_review         → private            author withdraws a pending submission
#   changes_requested → private            author withdraws
#   published         → private            author unpublishes (DELETE /agents/{id}/listing)
#
# Deliberately absent: anything → published other than from in_review. Approval is the
# only door into the store, so a bug elsewhere cannot publish by accident.
ALLOWED_TRANSITIONS: Dict[Optional[str], Set[str]] = {
    None: {"in_review"},
    "private": {"in_review"},
    "in_review": {"published", "changes_requested", "private"},
    "changes_requested": {"in_review", "private"},
    "published": {"taken_down", "changes_requested", "private"},
    "taken_down": {"in_review", "changes_requested"},
}

# States an author may drive. Everything else is the reviewer's (``require_admin``).
AUTHOR_TARGET_STATES: Set[str] = {"in_review", "private"}


class ListingTransitionError(ValueError):
    """An attempted listing state change that the machine does not allow."""

    def __init__(self, current: Optional[str], target: str, message: Optional[str] = None):
        self.current = current
        self.target = target
        super().__init__(message or self._default_message(current, target))

    @staticmethod
    def _default_message(current: Optional[str], target: str) -> str:
        if target not in LISTING_STATES:
            return (
                f"Unknown listing state '{target}'. Expected one of: "
                f"{', '.join(LISTING_STATES)}."
            )
        if current is None:
            return (
                f"This agent has never been submitted, so it cannot move to '{target}'. "
                "Submit it for review first."
            )
        allowed = ALLOWED_TRANSITIONS.get(current, set())
        allowed_text = ", ".join(sorted(allowed)) if allowed else "nothing"
        return (
            f"Cannot move a listing from '{current}' to '{target}'. "
            f"From '{current}' the allowed next states are: {allowed_text}."
        )


def assert_transition(current: Optional[str], target: str) -> None:
    """Raise ``ListingTransitionError`` unless ``current → target`` is a legal edge.

    ``current`` is ``None`` for a record with no ``listing`` block (never submitted).
    """
    if target not in LISTING_STATES:
        raise ListingTransitionError(current, target)
    if current is not None and current not in LISTING_STATES:
        # A state written by newer code that this deployment does not know. Refuse rather
        # than guess — the record is the source of truth and a wrong guess could publish.
        raise ListingTransitionError(
            current,
            target,
            f"This agent's listing is in an unrecognized state '{current}'. "
            "It may have been changed by a newer version; reload before retrying.",
        )
    if target not in ALLOWED_TRANSITIONS.get(current, set()):
        raise ListingTransitionError(current, target)


def is_published(state: Optional[str]) -> bool:
    """Whether a listing state means "visible in the store"."""
    return state == "published"


def gsi5_keys(state: Optional[str], category: Optional[str], created_at: str) -> Optional[Dict[str, str]]:
    """The sparse ``AgentDirectoryIndex`` (GSI5) key pair, or ``None`` when unlisted.

    ``GSI5_PK = LISTED#{category}`` / ``GSI5_SK = CREATED#{created_at}``, written **only**
    while ``state == "published"`` — the ``DueSyncIndex`` precedent on this same table.

    Returning ``None`` is the caller's signal to REMOVE both attributes, not to skip the
    write: leaving a stale key behind would keep a delisted agent queryable in the store,
    which is the exact failure the sparse index exists to prevent.

    ``created_at`` makes browse newest-first. A popularity sort would need a mutable sort
    key (a hot-item rewrite per use) and is deferred, not approximated — the store front
    is the manual ranking lever instead.
    """
    if not is_published(state):
        return None
    if not category:
        # Defensive: a published listing with no category has no shelf to sit on. The
        # service validates category at submit and at admin PATCH, so this is a
        # can't-happen that we refuse to paper over with a "LISTED#None" partition.
        raise ValueError("A published listing must carry a category.")
    return {
        "GSI5_PK": f"LISTED#{category}",
        "GSI5_SK": f"CREATED#{created_at}",
    }
