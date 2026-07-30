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
    ListingAuthorityError,
    ListingTransitionError,
    assert_author_target,
    assert_transition,
    gsi5_keys,
    is_listed,
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


def test_author_may_withdraw_a_pending_submission_outright():
    """Before publication, withdrawing is the author's alone — nobody else has it yet."""
    for state in ("in_review", "changes_requested"):
        assert_transition(state, "private")


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


# ── the edges that must not exist ────────────────────────────────────────────────────
def test_approval_is_the_only_door_into_the_store():
    """Only in_review may become published.

    This is the load-bearing assertion of the whole machine: if any other state could
    reach ``published``, a bug elsewhere could publish an agent nobody reviewed.
    """
    publishable = {s for s in ALLOWED_TRANSITIONS if "published" in ALLOWED_TRANSITIONS[s]}
    # ``withdrawal_requested`` is the one addition, and it is not a door *into* the store:
    # the listing never left, so declining a withdrawal is a refusal to unpublish rather
    # than a new publication. Nothing unreviewed can reach ``published`` through it — you
    # can only get to ``withdrawal_requested`` from ``published`` in the first place.
    assert publishable == {"in_review", "withdrawal_requested"}
    assert ALLOWED_TRANSITIONS["withdrawal_requested"] <= {"private", "published", "taken_down"}
    assert {s for s, t in ALLOWED_TRANSITIONS.items() if "withdrawal_requested" in t} == {"published"}


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
