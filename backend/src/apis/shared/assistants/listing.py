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

   Version snapshots extend that physics one level. The keys are now written on the
   published ``VERSION#`` row rather than on the Agent row, so the index cannot return
   *draft content* either — not because a reader checks, but because the draft has no row
   in the index. ``gsi5_keys`` is unchanged and still the single derivation; only its
   caller moved (``version_repository.set_version_index``).
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
    "withdrawal_requested",
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
#   changes_requested → private            author withdraws one that was never live
#   taken_down        → private            author shelves a delisted agent (so it can be
#                                          deleted — see below)
#   published         → withdrawal_requested author ASKS to pull a live listing
#   changes_requested → withdrawal_requested author ASKS to pull one that is *still* live
#   withdrawal_requested → changes_requested admin declines; it goes back where it came from
#   withdrawal_requested → private          admin grants the withdrawal
#   withdrawal_requested → published        admin declines; the listing stays live
#   withdrawal_requested → taken_down       admin pulls it outright instead
#
# Deliberately absent: anything → published other than from in_review or
# withdrawal_requested. Approval is the only door *into* the store, so a bug elsewhere
# cannot publish by accident; declining a withdrawal is not a new publication, it is a
# refusal to unpublish.
#
# ⚠️ ``published → private`` is deliberately GONE. An author could previously pull a live
# listing unilaterally, with no admin ever seeing it — the D2 review queue makes
# publication stop for a human, and unpublication should not be a side door around that.
# Withdrawal is now a request an admin acts on (see §5.1 of the version-snapshots spec).
# The edges an author still owns alone are the ones where nothing is on the shelf:
# ``private → in_review``, withdrawing a *pending* submission
# (``in_review``/``changes_requested`` → ``private``), and shelving one an admin has already
# pulled (``taken_down → private``). None of them removes anything users can currently see.
#
# ⚠️ ``taken_down → private`` was absent, and its absence was load-bearing for nothing.
# The stated reason was audit: the takedown record (``review_note``/``reviewed_by``/
# ``reviewed_at``) lives on the listing block itself, so letting an author reach ``private``
# lets them reach ``delete_assistant`` — which is refused for every state *except* ``private``
# — and take the record with them.
#
# That protection never held. ``taken_down → in_review → private`` was always walkable by the
# author alone (``submit_listing`` then ``withdraw_listing``: after a takedown
# ``published_version`` is cleared, so ``is_on_shelf`` is False and the withdrawal resolves to
# ``private`` immediately rather than to a request). The record was already erasable in three
# author-only steps; all the missing edge bought was that the middle step posted a submission
# to the D2 review queue that the author intended to withdraw a moment later. It taxed admins
# to protect nothing.
#
# So the edge is added and the audit question is named honestly rather than half-defended: if
# a takedown record must outlive the Agent, it needs a row that is not the Agent's own listing
# block, and no arrangement of this table can supply that. What the delete guard actually
# earns — and still earns — is that nothing *live or pending* is ever deleted out from under
# the store: ``published``, ``withdrawal_requested`` and ``in_review`` all still refuse, so
# unpublication stays an explicit act rather than a side effect of a delete.
#
# The edge is safe in the direction that matters. ``private`` is already an author target, and
# ``is_on_shelf`` hardcodes ``taken_down`` to False, so this can never route to
# ``withdrawal_requested`` and can never pull something off a shelf it is not on. Getting back
# *into* the store is unchanged: ``private → in_review → published``, approval still the only
# door.
#
# ⚠️ ``changes_requested`` covers two different listings — one that was never published, and
# one that *was* and is still serving while the author revises it (``review_listing``
# deliberately does not unpublish). Only the first may walk ``→ private`` alone; the second
# has to go through a request, which is why this row carries both exits and
# ``withdraw_listing`` picks between them with ``is_on_shelf`` rather than by state name.
#
# The reason that is safe — and it is the whole reason ``AgentListing.withdrawal_from``
# exists — is that declining a withdrawal returns the listing to the state it came *from*,
# not to a hardcoded ``published``. So ``in_review → changes_requested →
# withdrawal_requested → published`` is not reachable: a listing that entered
# ``withdrawal_requested`` from ``changes_requested`` can only go back to
# ``changes_requested``. Approval remains the only door into the store, and
# ``test_approval_is_the_only_door_into_the_store`` asserts exactly that.
ALLOWED_TRANSITIONS: Dict[Optional[str], Set[str]] = {
    None: {"in_review"},
    "private": {"in_review"},
    "in_review": {"published", "changes_requested", "private"},
    "changes_requested": {"in_review", "private", "withdrawal_requested"},
    "published": {"taken_down", "changes_requested", "withdrawal_requested"},
    "taken_down": {"in_review", "changes_requested", "private"},
    "withdrawal_requested": {"private", "published", "changes_requested", "taken_down"},
}

# States an author may drive. Everything else is the reviewer's (``require_admin``).
#
# ⚠️ This set was **dead** until the withdrawal work — declared and never read, so the
# comment above was aspirational rather than enforced. ``assert_author_target`` now uses it,
# because "an author cannot pull a live listing" deserves a real gate and not just an absent
# edge in the table. Both checks run on the author path: the table says the move is legal at
# all, this says the author is allowed to be the one making it.
AUTHOR_TARGET_STATES: Set[str] = {"in_review", "private", "withdrawal_requested"}

# Listing states whose Agent is live in the store.
#
# **Not the same question as ``state == "published"``, and the difference is the point of
# ``withdrawal_requested``.** A pending withdrawal request leaves the listing serving: the
# author has *asked* to pull it, an admin has not yet agreed, and dropping it off the shelf
# the moment they asked would hand the author exactly the unilateral delisting this state
# exists to prevent. So the store index stays written, and a declined request needs no
# repair.
LISTED_STATES: Set[str] = {"published", "withdrawal_requested"}

# Listing states waiting on an admin decision — what the Review queue shows and what the
# nav badge counts. §5.1 puts withdrawal requests in the *existing* queue rather than a
# second surface, on the grounds that a queue an admin has to remember to check is a queue
# that grows.
PENDING_DECISION_STATES: Set[str] = {"in_review", "withdrawal_requested"}


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


class ListingAuthorityError(ValueError):
    """An author attempted a transition that belongs to a reviewer."""


def assert_author_target(target: str) -> None:
    """Raise unless ``target`` is a state an author may drive themselves.

    Separate from ``assert_transition`` because they answer different questions, and
    collapsing them would lose the useful error. The table says whether the move is legal at
    all; this says whether the *author* gets to make it. ``published → private`` is now
    illegal for everyone (it is not in the table), while ``withdrawal_requested → private``
    is legal but an admin's alone.
    """
    if target not in AUTHOR_TARGET_STATES:
        raise ListingAuthorityError(
            f"Moving a listing to '{target}' is a reviewer's decision, not the author's."
        )


def is_published(state: Optional[str]) -> bool:
    """Whether a listing state is exactly ``published``.

    ⚠️ Usually **not** the question you want — see ``is_listed``. This is the narrow test
    for "approved and not under any pending request", and the only callers that should use
    it are ones deciding something about the approval itself.
    """
    return state == "published"


def is_listed(state: Optional[str]) -> bool:
    """Whether a listing state means "live in the store" (see ``LISTED_STATES``).

    True for ``withdrawal_requested`` as well as ``published``, because a requested
    withdrawal is not a granted one.

    ⚠️ **State-only, and therefore incomplete for "is this on the shelf right now?"** — use
    ``is_on_shelf`` for that. ``changes_requested`` is not in ``LISTED_STATES``, but a
    *published* listing that an admin sends back for changes deliberately keeps serving its
    approved version (``review_listing`` does not unpublish; a takedown is the operation
    that pulls something down). Such a listing is in ``changes_requested`` **and** in the
    store, so this predicate answers ``False`` about an Agent users can still see.

    Kept as-is rather than widened because two callers genuinely want the state alone:
    ``gsi5_keys``, which derives the index key at the moment of promotion, and the D13
    "does an admin edit need a new version" test.
    """
    return state in LISTED_STATES


def is_on_shelf(state: Optional[str], published_version: Optional[int]) -> bool:
    """Whether this listing is in the store *right now* — the fact, not the state name.

    ``published_version`` is the record of which snapshot carries the sparse index key, and
    every path that takes an Agent off the shelf clears it in the same breath as the key
    (``takedown_listing``, a granted withdrawal, a pre-publication withdraw). So the pointer
    being set is the same statement as "a version of this is queryable in the store" — which
    is the physics ``version_repository.set_version_index`` describes, asked as a question.

    Prefer this over ``is_listed`` anywhere the answer changes what a *user* can do. The
    case that forced it: an admin requesting changes on a live listing leaves it serving,
    but moves it to ``changes_requested``. Asked by state alone, ``withdraw_listing`` then
    reads that listing as not-live and sends the author straight to ``private`` — pulling a
    listing users can currently see, with no admin ever deciding. That is exactly the
    unilateral delisting ``withdrawal_requested`` exists to prevent, reached through the one
    state nobody thought to check.

    ``state`` is still consulted so a cleared-but-stale pointer cannot resurrect something:
    ``private`` and ``taken_down`` are never on the shelf whatever the pointer says.
    """
    if state in ("private", "taken_down", None):
        return False
    return published_version is not None


def gsi5_keys(state: Optional[str], category: Optional[str], created_at: str) -> Optional[Dict[str, str]]:
    """The sparse ``AgentDirectoryIndex`` (GSI5) key pair, or ``None`` when unlisted.

    ``GSI5_PK = LISTED#{category}`` / ``GSI5_SK = CREATED#{created_at}``, written **only**
    while the listing is live (``is_listed`` — ``published`` or ``withdrawal_requested``) —
    the ``DueSyncIndex`` precedent on this same table.

    Returning ``None`` is the caller's signal to REMOVE both attributes, not to skip the
    write: leaving a stale key behind would keep a delisted agent queryable in the store,
    which is the exact failure the sparse index exists to prevent.

    ``created_at`` makes browse newest-first. A popularity sort would need a mutable sort
    key (a hot-item rewrite per use) and is deferred, not approximated — the store front
    is the manual ranking lever instead.
    """
    if not is_listed(state):
        return None
    if not category:
        # Defensive: a listed agent with no category has no shelf to sit on. The service
        # validates category at submit and at admin PATCH, so this is a can't-happen that
        # we refuse to paper over with a "LISTED#None" partition.
        raise ValueError("A listed agent must carry a category.")
    return {
        "GSI5_PK": f"LISTED#{category}",
        "GSI5_SK": f"CREATED#{created_at}",
    }
