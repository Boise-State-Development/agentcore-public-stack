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
    LISTING_STATES,
    ListingTransitionError,
    assert_transition,
    gsi5_keys,
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


def test_author_may_withdraw_from_every_reachable_state():
    for state in ("in_review", "changes_requested", "published"):
        assert_transition(state, "private")


# ── the edges that must not exist ────────────────────────────────────────────────────
def test_approval_is_the_only_door_into_the_store():
    """Only in_review may become published.

    This is the load-bearing assertion of the whole machine: if any other state could
    reach ``published``, a bug elsewhere could publish an agent nobody reviewed.
    """
    publishable = {s for s in ALLOWED_TRANSITIONS if "published" in ALLOWED_TRANSITIONS[s]}
    assert publishable == {"in_review"}


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


@pytest.mark.parametrize("state", [s for s in LISTING_STATES if s != "published"] + [None])
def test_every_unpublished_state_yields_no_keys(state):
    """The sparse half of the sparse index — no key, so the store query can't see it."""
    assert gsi5_keys(state, "Teaching", CREATED) is None


def test_is_published_is_the_single_predicate():
    assert is_published("published")
    for state in [s for s in LISTING_STATES if s != "published"] + [None]:
        assert not is_published(state)


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
