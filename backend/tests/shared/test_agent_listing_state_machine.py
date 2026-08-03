"""Agent Marketplace Phase 1 — the listing state machine and the sparse GSI5 derivation.

The machine is pure, so these are exhaustive rather than representative: every ordered
pair of states is asserted legal or illegal, which is what keeps a later edit to the
transition table from quietly opening a path into the store.
"""

import itertools

import pytest

from apis.shared.assistants.listing import (
    ALLOWED_TRANSITIONS,
    DEFAULT_CATEGORIES,
    LISTED_STATES,
    LISTING_STATES,
    PENDING_DECISION_STATES,
    UPDATE_ORIGIN_STATES,
    ListingAuthorityError,
    ListingTransitionError,
    assert_author_target,
    assert_transition,
    author_cancel_target,
    gsi5_keys,
    is_listed,
    is_on_shelf,
    is_published,
)

CREATED = "2026-07-24T12:00:00.000000+00:00Z"


# ── the happy paths from D2's diagram ────────────────────────────────────────────────
def test_first_submission_from_no_listing_block():
    """A record with no listing block (the D3 backfill default) may be submitted."""
    assert_transition(None, "in_review")


def test_full_lifecycle_submit_approve_takedown():
    assert_transition(None, "in_review")
    assert_transition("in_review", "published")
    assert_transition("published", "taken_down")


def test_request_changes_then_resubmit():
    """The loop D2 draws: in_review → changes_requested → in_review."""
    assert_transition("in_review", "changes_requested")
    assert_transition("changes_requested", "in_review")


def test_resubmit_after_takedown():
    """The takedown dialog promises the author can resubmit once it's addressed."""
    assert_transition("taken_down", "in_review")


def test_a_published_listing_can_be_updated():
    """Since version snapshots, submitting again is the *only* way to change what users get.

    Edits to a published Agent land on the draft, so without this edge a published listing
    was a dead end: the author's choices were to request withdrawal (taking their own
    listing down to fix a typo) or to wait for an admin to send it back.
    """
    assert_transition("published", "in_review")


def test_author_may_withdraw_a_pending_submission_outright():
    """Before publication, withdrawing is the author's alone — nobody else has it yet."""
    for state in ("in_review", "changes_requested"):
        assert_transition(state, "private")


def test_author_may_shelve_a_taken_down_listing():
    """``taken_down → private``, and it is an author edge (both gates, not just the table).

    Added because ``delete_assistant`` refuses every state but ``private`` while telling the
    author of a taken-down agent to "take it back to private first" — advice for a door that
    did not exist. The only route out was ``taken_down → in_review → private``: resubmit for
    review purely to withdraw it a moment later, posting to the D2 queue to get somewhere the
    author was already allowed to be.
    """
    assert_transition("taken_down", "private")
    assert_author_target("private")


def test_shelving_a_takedown_does_not_become_a_withdrawal_request():
    """A takedown is already off the shelf, so there is nothing left to ask an admin for.

    ``withdraw_listing`` picks its target with ``is_on_shelf``, which hardcodes ``taken_down``
    to False whatever the pointer says. Asserted here because the new edge would be a real
    hazard if that were not true: routing a taken-down listing to ``withdrawal_requested``
    would park it in the admin queue forever over a listing nobody can see.
    """
    assert is_on_shelf("taken_down", None) is False
    assert is_on_shelf("taken_down", 7) is False, "a stale pointer must not resurrect it"
    with pytest.raises(ListingTransitionError):
        assert_transition("taken_down", "withdrawal_requested")


def test_shelving_a_takedown_is_not_a_way_back_into_the_store():
    """The new edge must not shorten the route to ``published``.

    ``taken_down → private`` is only useful if it stays a dead end for publication: from
    ``private`` the author still has to submit and an admin still has to approve. If this ever
    fails, the edge became a laundering step — pull it rather than patching the assertion.
    """
    assert ALLOWED_TRANSITIONS["private"] == {"in_review"}
    with pytest.raises(ListingTransitionError):
        assert_transition("private", "published")


def test_a_live_listing_can_only_be_requested_down():
    """§5.1 — an author cannot pull a live listing unilaterally.

    ``published → private`` was the side door around the review queue: publication stops for
    a human, and un-publication did not. Now it is a request an admin acts on.
    """
    with pytest.raises(ListingTransitionError):
        assert_transition("published", "private")
    assert_transition("published", "withdrawal_requested")


def test_an_admin_may_grant_or_decline_a_withdrawal():
    assert_transition("withdrawal_requested", "private")   # granted
    assert_transition("withdrawal_requested", "published")  # declined
    assert_transition("withdrawal_requested", "taken_down")  # pulled outright instead


def test_author_target_states_gate_the_authors_own_moves():
    """The set was dead until now — declared and never read. Assert it is load-bearing.

    ``withdrawal_requested → private`` is a legal edge but an admin's alone, so the table
    alone cannot express "the author may not do this". That is what the second gate is for.
    """
    assert_author_target("withdrawal_requested")
    assert_author_target("private")
    assert_author_target("in_review")
    for reviewer_only in ("published", "changes_requested", "taken_down"):
        with pytest.raises(ListingAuthorityError):
            assert_author_target(reviewer_only)


# ── cancelling a pending update ──────────────────────────────────────────────────────
@pytest.mark.parametrize("origin", sorted(UPDATE_ORIGIN_STATES))
def test_cancelling_an_update_returns_it_to_where_it_came_from(origin):
    """Not to ``published`` — the twin of ``withdrawal_from``, for the identical reason.

    A ``changes_requested`` listing that was published before the admin sent it back is
    live, so its author is updating something users can see. Cancelling that into
    ``published`` would discard the outstanding change request *and* publish a listing no
    admin approved.
    """
    assert author_cancel_target(origin) == origin
    assert_transition("in_review", origin)


def test_cancelling_a_first_submission_has_nowhere_to_return_to():
    """⚠️ The assertion that keeps this from being a second door into the store.

    ``None`` means the submission was not an update to a live listing. If this answered a
    default instead of raising, an author could walk their own unreviewed first submission
    into ``published``.
    """
    with pytest.raises(ListingAuthorityError):
        author_cancel_target(None)


@pytest.mark.parametrize(
    "state", [s for s in LISTING_STATES if s not in UPDATE_ORIGIN_STATES]
)
def test_only_an_on_shelf_origin_can_be_cancelled_back_to(state):
    with pytest.raises(ListingAuthorityError):
        author_cancel_target(state)


def test_every_cancel_target_is_reachable_from_in_review():
    """The gate returns a target rather than validating one, so the table must accept it.

    If an origin were ever added here without the matching edge, cancelling would raise a
    transition error from inside a path the author is entitled to walk.
    """
    for origin in UPDATE_ORIGIN_STATES:
        assert origin in ALLOWED_TRANSITIONS["in_review"]


def test_every_update_origin_is_a_state_that_can_be_on_the_shelf():
    """The other half of the contract, and what the service branch keys on.

    Cancelling *back into* a state that can never be live would put the record in a state
    claiming a published version it does not have.
    """
    for origin in UPDATE_ORIGIN_STATES:
        assert is_on_shelf(origin, 3), f"{origin} can never be live; it cannot be an origin"


# ── the edges that must not exist ────────────────────────────────────────────────────
def test_approval_is_the_only_door_into_the_store():
    """Only in_review may become published.

    This is the load-bearing assertion of the whole machine: if any other state could
    reach ``published``, a bug elsewhere could publish an agent nobody reviewed.
    """
    publishable = {s for s in ALLOWED_TRANSITIONS if "published" in ALLOWED_TRANSITIONS[s]}
    # ``withdrawal_requested`` is the one addition, and it is not a door *into* the store:
    # the listing never left, so declining a withdrawal is a refusal to unpublish rather
    # than a new publication.
    assert publishable == {"in_review", "withdrawal_requested"}
    assert ALLOWED_TRANSITIONS["withdrawal_requested"] <= {
        "private",
        "published",
        "changes_requested",
        "taken_down",
    }

    # The property that keeps the paragraph above true now that *two* states can reach
    # ``withdrawal_requested``: a request can only be declined back into a state that could
    # have sent it there. So `in_review → changes_requested → withdrawal_requested →
    # published` is not walkable — a listing that entered from ``changes_requested`` returns
    # to ``changes_requested``, and only one that was genuinely ``published`` returns to
    # ``published``.
    #
    # ⚠️ The table permits both exits; what picks the right one is
    # ``AgentListing.withdrawal_from``, read by ``decide_withdrawal``. If that field ever
    # stops being recorded on the way in, this invariant is no longer enforced by anything —
    # ``test_declining_returns_a_listing_to_the_state_it_came_from`` is the other half.
    entrants = {s for s, t in ALLOWED_TRANSITIONS.items() if "withdrawal_requested" in t}
    assert entrants == {"published", "changes_requested"}
    decline_targets = ALLOWED_TRANSITIONS["withdrawal_requested"] - {"private", "taken_down"}
    assert decline_targets == entrants, (
        "A declined withdrawal must be able to land exactly where requests come from — no "
        "more (that would be a new door into the store) and no less (that would strand one)."
    )


def test_private_cannot_jump_straight_to_published():
    with pytest.raises(ListingTransitionError):
        assert_transition("private", "published")


def test_never_submitted_cannot_jump_straight_to_published():
    with pytest.raises(ListingTransitionError) as exc:
        assert_transition(None, "published")
    assert "never been submitted" in str(exc.value)


def test_taken_down_cannot_return_to_published_without_review():
    with pytest.raises(ListingTransitionError):
        assert_transition("taken_down", "published")


def test_unknown_target_state_is_refused():
    with pytest.raises(ListingTransitionError) as exc:
        assert_transition("private", "listed")
    assert "Unknown listing state" in str(exc.value)


def test_unknown_current_state_is_refused_rather_than_guessed():
    """A state written by newer code must not be interpreted optimistically."""
    with pytest.raises(ListingTransitionError) as exc:
        assert_transition("archived", "in_review")
    assert "unrecognized state" in str(exc.value)


@pytest.mark.parametrize("current,target", list(itertools.product(LISTING_STATES, LISTING_STATES)))
def test_every_state_pair_matches_the_table(current, target):
    """Exhaustive: the enforcement and the declared table never diverge."""
    allowed = target in ALLOWED_TRANSITIONS.get(current, set())
    if allowed:
        assert_transition(current, target)
    else:
        with pytest.raises(ListingTransitionError):
            assert_transition(current, target)


def test_no_state_transitions_to_itself():
    for state in LISTING_STATES:
        assert state not in ALLOWED_TRANSITIONS.get(state, set())


# ── the sparse index derivation ──────────────────────────────────────────────────────
def test_published_yields_both_keys():
    keys = gsi5_keys("published", "Teaching", CREATED)
    assert keys == {"GSI5_PK": "LISTED#Teaching", "GSI5_SK": f"CREATED#{CREATED}"}


@pytest.mark.parametrize(
    "state", [s for s in LISTING_STATES if s not in LISTED_STATES] + [None]
)
def test_every_unlisted_state_yields_no_keys(state):
    """The sparse half of the sparse index — no key, so the store query can't see it."""
    assert gsi5_keys(state, "Teaching", CREATED) is None


def test_a_pending_withdrawal_keeps_its_keys():
    """§5.1 — the listing stays live while the request is pending.

    This is the whole reason ``is_listed`` exists separately from ``is_published``. If a
    withdrawal request dropped the keys, the author would have unilaterally delisted it
    just by asking, and a declined request would need the index rebuilt.
    """
    assert gsi5_keys("withdrawal_requested", "Teaching", CREATED) == {
        "GSI5_PK": "LISTED#Teaching",
        "GSI5_SK": f"CREATED#{CREATED}",
    }


def test_is_published_is_the_narrow_predicate():
    """``is_published`` means exactly ``published`` — usually not the question you want."""
    assert is_published("published")
    for state in [s for s in LISTING_STATES if s != "published"] + [None]:
        assert not is_published(state)


def test_is_listed_is_the_predicate_the_store_uses():
    for state in ("published", "withdrawal_requested"):
        assert is_listed(state)
    for state in [s for s in LISTING_STATES if s not in LISTED_STATES] + [None]:
        assert not is_listed(state)


def test_the_two_predicates_disagree_on_exactly_one_state():
    """If these ever coincide again, ``withdrawal_requested`` has silently stopped being live."""
    disagree = {s for s in LISTING_STATES if is_published(s) != is_listed(s)}
    assert disagree == {"withdrawal_requested"}


def test_published_without_a_category_refuses_rather_than_inventing_a_partition():
    with pytest.raises(ValueError, match="must carry a category"):
        gsi5_keys("published", None, CREATED)


def test_sort_key_is_created_at_so_browse_is_newest_first():
    older = gsi5_keys("published", "Research", "2026-01-01T00:00:00Z")
    newer = gsi5_keys("published", "Research", "2026-07-24T00:00:00Z")
    assert older["GSI5_PK"] == newer["GSI5_PK"]
    assert older["GSI5_SK"] < newer["GSI5_SK"]


def test_default_categories_are_non_empty_and_unique():
    assert DEFAULT_CATEGORIES
    assert len(set(DEFAULT_CATEGORIES)) == len(DEFAULT_CATEGORIES)
