"""Token bound on the protected tail — docs/specs/compaction-model-relative-thresholds.md §3.2.

The last ``protected_turns`` turns used to be kept whole whatever their size,
and a conversation no older than that was never cut at all. A 2-turn
conversation that read a 166-page PDF sat at 343k–762k input on a 1M model
with compaction never firing. Now the ceiling bounds the tail: when the tail
alone exceeds it, the protection shrinks to the newest whole turns that fit
(never below the newest turn), and a young conversation is cut once it
reaches the hard ceiling.

Fixed policy throughout (window unknown, threshold 1000): ceiling 1000,
floor 250, hard ceiling 1500. ``history_tokens`` is left ``None`` so the
per-message estimates are calibrated to the input count.
"""

import copy
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from agents.main_agent.session.compaction_models import CompactionConfig, CompactionState
from agents.main_agent.session.compaction_policy import choose_checkpoint

from .conftest import make_assistant_message, make_user_message


CONFIG = dict(enabled=True, token_threshold=1000, protected_turns=3, summary_token_budget=200)


def _now():
    return datetime.now(timezone.utc)


def _dump(messages):
    return json.dumps(messages, sort_keys=True)


def _turns(*sizes):
    """One user/assistant pair per size; the user message carries ``size`` chars."""
    messages = []
    for i, size in enumerate(sizes):
        messages.append(make_user_message(f"q{i} " + "u" * size))
        messages.append(make_assistant_message(f"a{i}"))
    return messages


def _manager(make_session_manager, store, **overrides):
    cfg = CompactionConfig(**{**CONFIG, **overrides})
    mgr = make_session_manager(compaction_config=cfg)
    mgr._load_compaction_state = lambda: CompactionState.from_dict(store.get("compaction"))

    def _save(state, record_event=False):
        state.updated_at = _now().isoformat()
        store["compaction"] = state.to_dict()

    mgr._save_compaction_state = _save
    mgr._retrieve_session_summaries = lambda: ["LTM summary"]
    mgr.compaction_state = CompactionState.from_dict(store.get("compaction"))
    return mgr


def _agent(messages):
    agent = MagicMock()
    agent.messages = messages
    return agent


class TestRolloutSwitch:
    def test_default_on_and_only_literal_false_disables(self, monkeypatch):
        name = "AGENTCORE_MEMORY_COMPACTION_TAIL_TOKEN_BOUND_ENABLED"
        monkeypatch.delenv(name, raising=False)
        assert CompactionConfig.from_env().tail_token_bound_enabled is True
        monkeypatch.setenv(name, "")
        assert CompactionConfig.from_env().tail_token_bound_enabled is True
        monkeypatch.setenv(name, "FALSE")
        assert CompactionConfig.from_env().tail_token_bound_enabled is False


# ---------------------------------------------------------------------------
# choose_checkpoint
# ---------------------------------------------------------------------------

class TestChooseCheckpointTailBound:
    def test_tail_within_bound_keeps_minimum_protection(self):
        msgs = _turns(40, 40, 40, 400, 400, 400)
        cutoffs = [0, 2, 4, 6, 8, 10]
        cut, retained = choose_checkpoint(msgs, cutoffs, 3, floor_tokens=100, history_tokens=600, tail_bound_tokens=1000)
        assert cut == 6  # cutoffs[-3], exactly as without a bound

    def test_tail_over_bound_shrinks_to_newest_turns_that_fit(self):
        # Turn 4 (index 6) is huge; turns 5 and 6 together fit the bound.
        msgs = _turns(40, 40, 40, 8000, 40, 40)
        cutoffs = [0, 2, 4, 6, 8, 10]
        cut, retained = choose_checkpoint(msgs, cutoffs, 3, floor_tokens=10, history_tokens=3000, tail_bound_tokens=1000)
        assert cut == 8
        assert retained is not None and retained <= 1000

    def test_keeps_as_many_newest_turns_as_fit(self):
        # Turns 4–6 are 100 each, turn 3 huge; bound admits all three small ones
        # only when the big one goes — and protected_turns=4 put it in the tail.
        msgs = _turns(40, 40, 8000, 400, 400, 400)
        cutoffs = [0, 2, 4, 6, 8, 10]
        cut, _ = choose_checkpoint(msgs, cutoffs, 4, floor_tokens=10, history_tokens=None, tail_bound_tokens=1000)
        assert cut == 6  # keeps three newest turns, drops only the oversized one

    def test_newest_turn_alone_over_bound_keeps_exactly_that_turn(self):
        msgs = _turns(400, 400, 8000)
        cutoffs = [0, 2, 4]
        cut, retained = choose_checkpoint(msgs, cutoffs, 3, floor_tokens=10, history_tokens=None, tail_bound_tokens=100)
        assert cut == cutoffs[-1]
        assert retained > 100  # over the bound, but the current turn always survives

    def test_young_conversation_needs_the_bound(self):
        msgs = _turns(8000, 40)
        assert choose_checkpoint(msgs, [0, 2], 3, floor_tokens=10, history_tokens=None) == (0, None)
        cut, _ = choose_checkpoint(msgs, [0, 2], 3, floor_tokens=10, history_tokens=None, tail_bound_tokens=500)
        assert cut == 2

    def test_young_conversation_under_the_bound_is_not_cut(self):
        msgs = _turns(400, 40)
        assert choose_checkpoint(msgs, [0, 2], 3, floor_tokens=10, history_tokens=None, tail_bound_tokens=10_000) == (0, None)

    def test_single_turn_is_never_cut(self):
        msgs = _turns(80_000)
        assert choose_checkpoint(msgs, [0], 3, floor_tokens=10, history_tokens=None, tail_bound_tokens=100) == (0, None)

    def test_floor_still_wins_when_reachable(self):
        # A deep cut under the floor is found before the tail is ever considered.
        msgs = _turns(8000, 8000, 8000, 40, 40, 40)
        cutoffs = [0, 2, 4, 6, 8, 10]
        cut, _ = choose_checkpoint(msgs, cutoffs, 3, floor_tokens=200, history_tokens=None, tail_bound_tokens=1000)
        assert cut == 6

    def test_is_a_pure_function_of_its_inputs(self):
        msgs = _turns(40, 40, 40, 8000, 40, 40)
        args = (msgs, [0, 2, 4, 6, 8, 10], 3, 10, 3000)
        assert choose_checkpoint(*args, tail_bound_tokens=1000) == choose_checkpoint(copy.deepcopy(msgs), *args[1:], tail_bound_tokens=1000)


# ---------------------------------------------------------------------------
# update_after_turn — a young conversation over the hard ceiling
# ---------------------------------------------------------------------------

class TestYoungConversation:
    @pytest.mark.asyncio
    async def test_turn_two_above_hard_ceiling_compacts_before_turn_three(self, make_session_manager):
        store = {}
        mgr = _manager(make_session_manager, store)
        live = _turns(8000, 40)  # turn 1 read a document; turn 2 is small
        stored = copy.deepcopy(live)

        result = await mgr.update_after_turn(2100, current_messages=live)  # >= hard 1500
        assert result is not None and result.deferred is True
        assert result.forced is False  # armed: a large cut, not the spiral signal
        state = mgr.compaction_state
        assert state.pending_checkpoint == 2 and state.checkpoint == 0
        assert state.policy["tailBounded"] is True and state.policy["keptTurns"] == 1
        assert _dump(live) == _dump(stored)  # nothing moves at turn end

        # Head of turn 3: the parked cut is unaffordable to wait on and lands.
        assert mgr.apply_pending_compaction(_agent(live), prefix_key="m|default") == "hard_ceiling"
        assert mgr.compaction_state.checkpoint == 2
        assert mgr.compaction_state.truncation_anchor == 2
        assert len(live) == 2
        # The newest turn's own user message survived, behind the summary.
        head_text = live[0]["content"][0]["text"]
        assert "conversation_summary" in head_text and "LTM summary" in head_text
        assert "q1 " in _dump(live[0])
        # Everything the cut retains after the head is byte-identical.
        assert _dump(live[1:]) == _dump(stored[3:])

    @pytest.mark.asyncio
    async def test_live_cut_matches_a_cold_restore(self, make_session_manager):
        """Prompt-cache contract: after the cut, a warm agent and a cold restore
        of the same stored history send identical bytes."""
        store = {}
        mgr = _manager(make_session_manager, store)
        live = _turns(8000, 40)
        stored = copy.deepcopy(live)
        await mgr.update_after_turn(2100, current_messages=live)
        assert mgr.apply_pending_compaction(_agent(live), prefix_key="m|default") == "hard_ceiling"

        cold = _manager(make_session_manager, store)
        agent = _agent(copy.deepcopy(stored))
        cold._apply_compaction(agent)
        assert _dump(agent.messages) == _dump(live)
        assert cold._live_offset == mgr._live_offset == 2

        # And the restore stays byte-stable until the next state change.
        again = _manager(make_session_manager, store)
        agent2 = _agent(copy.deepcopy(stored))
        again._apply_compaction(agent2)
        assert _dump(agent2.messages) == _dump(agent.messages)

    @pytest.mark.asyncio
    async def test_between_ceiling_and_hard_ceiling_is_not_cut(self, make_session_manager):
        store = {}
        mgr = _manager(make_session_manager, store)
        assert await mgr.update_after_turn(1200, current_messages=_turns(4000, 40)) is None
        assert mgr.compaction_state.pending_checkpoint is None and mgr.compaction_state.checkpoint == 0

    @pytest.mark.asyncio
    async def test_one_turn_over_the_hard_ceiling_has_nothing_to_cut(self, make_session_manager):
        store = {}
        mgr = _manager(make_session_manager, store)
        assert await mgr.update_after_turn(5000, current_messages=_turns(20_000)) is None
        assert store["compaction"]["checkpoint"] == 0
        assert store["compaction"]["lastInputTokens"] == 5000

    @pytest.mark.asyncio
    async def test_rollout_switch_off_restores_whole_tail_protection(self, make_session_manager):
        store = {}
        mgr = _manager(make_session_manager, store, tail_token_bound_enabled=False)
        assert await mgr.update_after_turn(2100, current_messages=_turns(8000, 40)) is None
        assert mgr.compaction_state.pending_checkpoint is None

    @pytest.mark.asyncio
    async def test_legacy_policy_is_unchanged(self, make_session_manager):
        store = {}
        mgr = _manager(make_session_manager, store, model_relative_enabled=False)
        assert await mgr.update_after_turn(5000, current_messages=_turns(8000, 40)) is None


# ---------------------------------------------------------------------------
# update_after_turn — an older conversation whose tail is over the bound
# ---------------------------------------------------------------------------

class TestOversizedTail:
    @pytest.mark.asyncio
    async def test_tail_shrinks_by_whole_turns(self, make_session_manager):
        store = {}
        mgr = _manager(make_session_manager, store, deferred_apply_enabled=False)
        live = _turns(40, 40, 8000, 40, 40)  # cutoffs [0,2,4,6,8]; the big turn is inside the tail
        result = await mgr.update_after_turn(2500, current_messages=live)
        assert result is not None
        assert mgr.compaction_state.checkpoint == 6  # keeps the two newest turns
        assert mgr.compaction_state.policy["keptTurns"] == 2
        assert mgr.compaction_state.policy["tailBounded"] is True

    @pytest.mark.asyncio
    async def test_cut_record_counts_a_bounded_tail(self, make_session_manager):
        store = {}
        mgr = _manager(make_session_manager, store, deferred_apply_enabled=False)
        emitted = []
        mgr._emit_emf = lambda metrics, props, units=None: emitted.append(metrics)
        await mgr.update_after_turn(2500, current_messages=_turns(40, 40, 8000, 40, 40))
        cut = next(m for m in emitted if "CompactionCut" in m)
        assert cut["CompactionTailBounded"] == 1

    @pytest.mark.asyncio
    async def test_switch_off_takes_the_minimum_protection_cut(self, make_session_manager):
        store = {}
        mgr = _manager(make_session_manager, store, deferred_apply_enabled=False, tail_token_bound_enabled=False)
        await mgr.update_after_turn(2500, current_messages=_turns(40, 40, 8000, 40, 40))
        assert mgr.compaction_state.checkpoint == 4  # cutoffs[-3]
        assert mgr.compaction_state.policy["tailBounded"] is False
        assert mgr.compaction_state.policy["keptTurns"] == 3

    @pytest.mark.asyncio
    async def test_cut_within_the_bound_is_not_marked(self, make_session_manager):
        store = {}
        mgr = _manager(make_session_manager, store, deferred_apply_enabled=False)
        await mgr.update_after_turn(1200, current_messages=_turns(400, 400, 400, 400, 400))
        assert mgr.compaction_state.checkpoint == 4
        assert mgr.compaction_state.policy["tailBounded"] is False


# ---------------------------------------------------------------------------
# Two agent instances serving one session
# ---------------------------------------------------------------------------

class TestMonotonicAcrossInstances:
    @pytest.mark.asyncio
    async def test_sibling_computes_the_same_cut_and_never_moves_it_back(self, make_session_manager):
        store = {}
        live = _turns(8000, 40)
        a = _manager(make_session_manager, store, deferred_apply_enabled=False)
        b = _manager(make_session_manager, store, deferred_apply_enabled=False)  # built before A cut

        assert (await a.update_after_turn(2100, current_messages=copy.deepcopy(live))) is not None
        assert store["compaction"]["checkpoint"] == 2 and store["compaction"]["truncationAnchor"] == 2

        # B serves the next post-turn on the same history: it adopts A's state,
        # derives the same cut, and so does not advance or regress anything.
        assert (await b.update_after_turn(2100, current_messages=copy.deepcopy(live))) is None
        assert store["compaction"]["checkpoint"] == 2 and store["compaction"]["truncationAnchor"] == 2

        # Under the ceiling, B's save still carries A's anchor forward.
        assert (await b.update_after_turn(500, current_messages=copy.deepcopy(live))) is None
        assert store["compaction"]["checkpoint"] == 2 and store["compaction"]["truncationAnchor"] == 2

    @pytest.mark.asyncio
    async def test_persisted_anchor_ahead_of_the_cut_is_kept(self, make_session_manager):
        store = {"compaction": CompactionState(truncation_anchor=3, updated_at=_now().isoformat()).to_dict()}
        mgr = _manager(make_session_manager, store, deferred_apply_enabled=False)
        await mgr.update_after_turn(2100, current_messages=_turns(8000, 40))
        assert store["compaction"]["checkpoint"] == 2
        assert store["compaction"]["truncationAnchor"] == 3
