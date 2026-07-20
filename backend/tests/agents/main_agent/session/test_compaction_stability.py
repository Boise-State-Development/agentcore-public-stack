"""Byte-stability tests for restore-time compaction.

Bedrock prompt caching requires an exact prefix match, so
``TurnBasedSessionManager.initialize()`` must derive the restored history as
a pure function of (stored messages, persisted compaction state). The old
design truncated tool contents behind a sliding protected-turns window,
which re-mutated the turn that just aged past the window on every restore —
breaking the cached prefix and forcing a full prefix re-write (~$2.5/MTok on
a 35k–150k prefix) nearly every turn (observed in prod session aecd387d:
-382/-1035/-1513 inter-turn prefix-token shrinkages with cacheRead=0 well
inside the cache TTL).

These tests pin the redesign: truncation is driven only by the persisted
``truncation_anchor``, which moves at checkpoint advances (where the slice
already pays the one cache re-write) or opportunistically when the prompt
cache has already expired between turns.
"""

import copy
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from agents.main_agent.session.compaction_models import CompactionConfig, CompactionState
from agents.main_agent.session.turn_based_session_manager import TurnBasedSessionManager

from .conftest import (
    make_user_message,
    make_assistant_message,
    make_tool_use_message,
    make_tool_result_message,
)


LONG_RESULT = "R" * 400  # well above the fixture's max_tool_content_length=50


def make_tool_turn(i: int) -> list:
    """One 4-message turn with a tool result long enough to be truncatable."""
    return [
        make_user_message(f"Question {i}"),
        make_tool_use_message(f"t{i}", "search", {"q": f"query {i}"}),
        make_tool_result_message(f"t{i}", LONG_RESULT),
        make_assistant_message(f"Answer {i}"),
    ]


def make_tool_conversation(turns: int) -> list:
    messages = []
    for i in range(turns):
        messages.extend(make_tool_turn(i))
    return messages


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _dump(messages) -> str:
    return json.dumps(messages, sort_keys=True)


def _fresh_state(**overrides) -> dict:
    """Persisted state stamped 'just now' — the prompt cache is still warm."""
    return {"compaction": CompactionState(updated_at=_iso(_now()), **overrides).to_dict()}


def _stale_state(age_seconds: int = 600, **overrides) -> dict:
    """Persisted state older than the cache TTL — the cache has expired."""
    return {
        "compaction": CompactionState(
            updated_at=_iso(_now() - timedelta(seconds=age_seconds)), **overrides
        ).to_dict()
    }


@pytest.fixture
def restore_session(make_session_manager, compaction_config):
    """Simulate one full session restore against an in-memory state store.

    Each call builds a fresh manager (as a new container invocation would),
    wires ``_load/_save_compaction_state`` to ``state_store`` so persistence
    behaves like the sessions-metadata record, and runs the real
    ``initialize()`` restore path (sanitize + compaction + repair). Returns
    the derived ``agent.messages``.
    """

    def _restore(state_store: dict, stored_messages: list):
        mgr = make_session_manager(compaction_config=compaction_config)
        mgr._load_compaction_state = lambda: CompactionState.from_dict(
            state_store.get("compaction")
        )

        def _save(state: CompactionState) -> None:
            state.updated_at = _iso(_now())
            state_store["compaction"] = state.to_dict()

        mgr._save_compaction_state = _save
        mgr._retrieve_session_summaries = lambda: []

        session_agent = MagicMock()
        session_agent.state = {}
        session_agent.conversation_manager_state = {}
        session_messages = []
        for msg in copy.deepcopy(stored_messages):
            sm = MagicMock()
            sm.to_message.return_value = msg
            session_messages.append(sm)

        mgr.read_agent = MagicMock(return_value=session_agent)
        mgr.list_messages = MagicMock(return_value=session_messages)
        mgr._is_new_session = False

        agent = MagicMock()
        agent.agent_id = "default"
        agent.messages = []
        agent.conversation_manager.restore_from_session.return_value = []
        agent.conversation_manager.removed_message_count = 0

        mgr.initialize(agent)
        return agent.messages

    return _restore


@pytest.fixture
def make_wired_manager(make_session_manager, compaction_config):
    """Manager wired to an in-memory state store, for update_after_turn tests."""

    def _make(state_store: dict):
        mgr = make_session_manager(compaction_config=compaction_config)
        mgr._load_compaction_state = lambda: CompactionState.from_dict(
            state_store.get("compaction")
        )

        def _save(state: CompactionState) -> None:
            state.updated_at = _iso(_now())
            state_store["compaction"] = state.to_dict()

        mgr._save_compaction_state = _save
        mgr._retrieve_session_summaries = lambda: []
        return mgr

    return _make


class TestByteStability:
    """agent.messages must be byte-identical across consecutive restores
    when no compaction-state change occurs."""

    def test_consecutive_restores_identical_no_state(self, restore_session):
        stored = make_tool_conversation(6)
        store = {}
        first = restore_session(store, stored)
        second = restore_session(store, stored)
        assert _dump(first) == _dump(second)
        # Nothing may be truncated: no anchor has ever been set.
        assert "[truncated" not in _dump(first)

    def test_consecutive_restores_identical_warm_cache(self, restore_session):
        stored = make_tool_conversation(8)
        store = _fresh_state()
        first = restore_session(store, stored)
        second = restore_session(store, stored)
        assert _dump(first) == _dump(second)
        assert "[truncated" not in _dump(first)

    def test_prefix_stable_as_turns_accumulate(self, restore_session):
        """The regression the sliding window caused: appending a new turn must
        not mutate any earlier message. Under the old design the turn that
        aged past protected_turns was newly truncated here, breaking the
        cached prefix on every turn."""
        stored = make_tool_conversation(6)
        before = restore_session(_fresh_state(), stored)

        stored_next = stored + make_tool_turn(6)
        # A real turn refreshes updated_at within the TTL.
        after = restore_session(_fresh_state(), stored_next)

        assert _dump(after[: len(before)]) == _dump(before)

    def test_checkpointed_restore_is_stable(self, restore_session):
        """With a persisted checkpoint + summary, consecutive restores still
        derive the identical history (summary prepend is deterministic)."""
        stored = make_tool_conversation(8)
        checkpoint = 5 * 4  # start of turn 5
        store = _fresh_state(
            checkpoint=checkpoint,
            truncation_anchor=checkpoint,
            summary="Earlier discussion about queries 0-4.",
        )
        first = restore_session(store, stored)
        second = restore_session(store, stored)
        assert _dump(first) == _dump(second)
        assert len(first) == len(stored) - checkpoint
        assert "<conversation_summary>" in first[0]["content"][0]["text"]
        assert "[truncated" not in _dump(first)

    def test_legacy_state_without_anchor_is_stable_and_untruncated(self, restore_session):
        """Records written before truncationAnchor existed default the anchor
        to the checkpoint: retained history is never truncated."""
        stored = make_tool_conversation(8)
        legacy = {
            "checkpoint": 20,
            "summary": "Legacy summary",
            "lastInputTokens": 120_000,
            "updatedAt": _iso(_now()),
            "totalSummarizedTurns": 5,
        }
        store = {"compaction": legacy}
        first = restore_session(store, stored)
        second = restore_session(store, stored)
        assert _dump(first) == _dump(second)
        assert "[truncated" not in _dump(first)


class TestAnchorTruncation:
    def test_truncation_applies_only_below_anchor(self, restore_session):
        stored = make_tool_conversation(6)
        anchor = 3 * 4  # start of turn 3
        store = _fresh_state(truncation_anchor=anchor)
        derived = restore_session(store, stored)

        assert "[truncated" in _dump(derived[:anchor])
        assert "[truncated" not in _dump(derived[anchor:])
        # And the derivation is stable.
        assert _dump(restore_session(store, stored)) == _dump(derived)

    def test_anchor_beyond_history_is_clamped(self, restore_session):
        stored = make_tool_conversation(2)
        store = _fresh_state(truncation_anchor=999)
        derived = restore_session(store, stored)
        assert len(derived) == len(stored)
        assert "[truncated" in _dump(derived)


class TestOpportunisticAnchorAdvance:
    def test_expired_cache_advances_and_persists_anchor(self, restore_session):
        """When the prompt cache has already expired (>TTL since the previous
        turn) the anchor slides to the protected-turns boundary for free and
        is persisted, so the very next restore derives identical history."""
        stored = make_tool_conversation(8)
        store = _stale_state(age_seconds=600)
        first = restore_session(store, stored)

        expected_anchor = (8 - 3) * 4  # cutoffs[-3] = start of turn 5
        assert store["compaction"]["truncationAnchor"] == expected_anchor
        assert "[truncated" in _dump(first[:expected_anchor])
        assert "[truncated" not in _dump(first[expected_anchor:])

        # The save stamped updated_at=now, so the follow-up restore is inside
        # the TTL: no further movement, byte-identical output.
        second = restore_session(store, stored)
        assert store["compaction"]["truncationAnchor"] == expected_anchor
        assert _dump(first) == _dump(second)

    def test_warm_cache_never_advances_anchor(self, restore_session):
        stored = make_tool_conversation(8)
        store = _fresh_state()
        restore_session(store, stored)
        assert store["compaction"]["truncationAnchor"] == 0

    def test_no_advance_when_too_few_turns(self, restore_session):
        stored = make_tool_conversation(3)
        store = _stale_state(age_seconds=600)
        derived = restore_session(store, stored)
        assert store["compaction"]["truncationAnchor"] == 0
        assert "[truncated" not in _dump(derived)

    def test_missing_updated_at_treated_as_warm(self, restore_session):
        stored = make_tool_conversation(8)
        store = {"compaction": CompactionState().to_dict()}  # updated_at=None
        restore_session(store, stored)
        assert store["compaction"]["truncationAnchor"] == 0


class TestCacheWindowExpired:
    def test_none_and_garbage_are_not_expired(self):
        assert TurnBasedSessionManager._cache_window_expired(None, 300) is False
        assert TurnBasedSessionManager._cache_window_expired("not-a-timestamp", 300) is False

    def test_z_suffix_and_naive_timestamps_parse(self):
        old = (_now() - timedelta(seconds=900)).strftime("%Y-%m-%dT%H:%M:%S")
        assert TurnBasedSessionManager._cache_window_expired(old + "Z", 300) is True
        assert TurnBasedSessionManager._cache_window_expired(old, 300) is True  # naive → UTC

    def test_recent_timestamp_not_expired(self):
        assert TurnBasedSessionManager._cache_window_expired(_iso(_now()), 300) is False


class TestCheckpointAdvanceMovesAnchor:
    @pytest.mark.asyncio
    async def test_update_after_turn_sets_anchor_with_checkpoint(
        self, make_wired_manager, restore_session
    ):
        stored = make_tool_conversation(8)
        store = _fresh_state()
        mgr = make_wired_manager(store)

        result = await mgr.update_after_turn(input_tokens=5000, current_messages=stored)

        expected_checkpoint = (8 - 3) * 4
        assert result is not None
        assert result.new_checkpoint == expected_checkpoint
        assert mgr.compaction_state.checkpoint == expected_checkpoint
        assert mgr.compaction_state.truncation_anchor == expected_checkpoint
        assert store["compaction"]["truncationAnchor"] == expected_checkpoint

        # Post-advance restores are byte-stable and truncate nothing the
        # slice retained (anchor == checkpoint).
        first = restore_session(store, stored)
        second = restore_session(store, stored)
        assert _dump(first) == _dump(second)
        assert "[truncated" not in _dump(first)

    @pytest.mark.asyncio
    async def test_anchor_never_regresses_below_prior_anchor(self, make_wired_manager):
        stored = make_tool_conversation(8)
        anchor = (8 - 3) * 4 + 4  # ahead of where the checkpoint will land
        store = _fresh_state(truncation_anchor=anchor)
        mgr = make_wired_manager(store)

        result = await mgr.update_after_turn(input_tokens=5000, current_messages=stored)
        assert result is not None
        assert mgr.compaction_state.truncation_anchor == anchor

    @pytest.mark.asyncio
    async def test_below_threshold_saves_state_without_movement(self, make_wired_manager):
        stored = make_tool_conversation(8)
        store = _fresh_state(truncation_anchor=8, checkpoint=4)
        mgr = make_wired_manager(store)

        result = await mgr.update_after_turn(input_tokens=10, current_messages=stored)
        assert result is None
        assert store["compaction"]["checkpoint"] == 4
        assert store["compaction"]["truncationAnchor"] == 8
