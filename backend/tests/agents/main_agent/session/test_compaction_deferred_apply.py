"""Paid-when-free compaction — docs/specs/compaction-model-relative-thresholds.md §3.5.

Post-turn parks the cut as PENDING; the head of the next turn applies it to
the live list in place only when the prefix re-write is free (cache expired,
model/agent switched) or unavoidable (hard ceiling).
"""

import copy
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from agents.main_agent.session.compaction_models import CompactionConfig, CompactionState
from agents.main_agent.session.compaction_summary import approx_tokens

from .conftest import make_conversation


CONFIG = dict(enabled=True, token_threshold=1000, protected_turns=3, summary_token_budget=200)


def _now():
    return datetime.now(timezone.utc)


def _dump(messages):
    return json.dumps(messages, sort_keys=True)


def _manager(make_session_manager, store, **overrides):
    """Manager wired to an in-memory state store (deferred mode ON)."""
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


def _age(store, seconds):
    store["compaction"]["updatedAt"] = (_now() - timedelta(seconds=seconds)).isoformat()


class TestPostTurnParksTheCut:
    @pytest.mark.asyncio
    async def test_over_ceiling_parks_pending_and_leaves_checkpoint(self, make_session_manager):
        store = {}
        mgr = _manager(make_session_manager, store)
        live = make_conversation(5)
        result = await mgr.update_after_turn(2000, current_messages=live)
        assert result is not None and result.deferred is True
        state = mgr.compaction_state
        assert state.pending_checkpoint == 4 and state.checkpoint == 0
        assert state.pending_summary == "LTM summary"
        assert state.pending_hard_ceiling == 1500
        assert state.armed is False
        assert store["compaction"]["pendingCheckpoint"] == 4
        assert state.policy["deferred"] is True
        # Nothing about the live list changed at turn end.
        assert len(live) == 10 and "conversation_summary" not in _dump(live)

    @pytest.mark.asyncio
    async def test_second_over_ceiling_turn_does_not_cut_deeper(self, make_session_manager):
        store = {}
        mgr = _manager(make_session_manager, store)
        await mgr.update_after_turn(2000, current_messages=make_conversation(5))
        second = await mgr.update_after_turn(2500, current_messages=make_conversation(7))  # even at hard ceiling
        assert second is None
        assert mgr.compaction_state.pending_checkpoint == 4

    @pytest.mark.asyncio
    async def test_kill_switch_applies_immediately(self, make_session_manager):
        store = {}
        mgr = _manager(make_session_manager, store, deferred_apply_enabled=False)
        result = await mgr.update_after_turn(2000, current_messages=make_conversation(5))
        assert result.deferred is False
        assert mgr.compaction_state.checkpoint == 4 and mgr.compaction_state.pending_checkpoint is None

    @pytest.mark.asyncio
    async def test_legacy_mode_applies_immediately(self, make_session_manager):
        store = {}
        mgr = _manager(make_session_manager, store, model_relative_enabled=False)
        result = await mgr.update_after_turn(2000, current_messages=make_conversation(5))
        assert result.deferred is False and mgr.compaction_state.checkpoint == 4


class TestHeadOfTurnApply:
    async def _parked(self, make_session_manager, store):
        """Park a cut at 1200 tokens: over the ceiling (1000), under hard (1500)."""
        mgr = _manager(make_session_manager, store)
        live = make_conversation(5)
        await mgr.update_after_turn(1200, current_messages=live)
        assert mgr.compaction_state.pending_checkpoint == 4
        return mgr, live

    @pytest.mark.asyncio
    async def test_warm_cache_same_prefix_below_hard_waits(self, make_session_manager):
        store = {}
        mgr, live = await self._parked(make_session_manager, store)
        before = _dump(live)
        assert mgr.apply_pending_compaction(_agent(live), prefix_key="m|default") is None
        assert _dump(live) == before
        assert mgr.compaction_state.pending_checkpoint == 4 and mgr.compaction_state.checkpoint == 0

    @pytest.mark.asyncio
    async def test_cache_expired_applies_in_place(self, make_session_manager):
        store = {}
        mgr, live = await self._parked(make_session_manager, store)
        _age(store, 600)
        list_id = id(live)
        reason = mgr.apply_pending_compaction(_agent(live), prefix_key="m|default")
        assert reason == "cache_expired"
        assert id(live) == list_id  # slice assignment, never rebound
        assert len(live) == 6  # 10 - 4
        assert live[0]["role"] == "user" and "conversation_summary" in live[0]["content"][0]["text"]
        assert "LTM summary" in live[0]["content"][0]["text"]
        state = mgr.compaction_state
        assert state.checkpoint == 4 and state.truncation_anchor == 4 and state.summary == "LTM summary"
        assert state.pending_checkpoint is None and state.pending_summary is None
        assert mgr._live_offset == 4
        assert state.policy["applied"] == "cache_expired"
        assert state.policy["cacheGapSeconds"] >= 599
        assert store["compaction"]["checkpoint"] == 4 and store["compaction"]["pendingCheckpoint"] is None

    @pytest.mark.asyncio
    async def test_prefix_change_applies(self, make_session_manager):
        store = {}
        mgr = _manager(make_session_manager, store)
        live = make_conversation(5)
        # Turn 1 stamps the key; turn end persists it.
        mgr.apply_pending_compaction(_agent(live), prefix_key="sonnet|default")
        await mgr.update_after_turn(1200, current_messages=live)
        assert store["compaction"]["lastPrefixKey"] == "sonnet|default"
        # Same key, warm: waits. Different key (model switch): applies.
        assert mgr.apply_pending_compaction(_agent(live), prefix_key="sonnet|default") is None
        assert mgr.apply_pending_compaction(_agent(live), prefix_key="opus|default") == "prefix_changed"
        assert len(live) == 6

    @pytest.mark.asyncio
    async def test_first_ever_prefix_key_is_not_a_change(self, make_session_manager):
        store = {}
        mgr, live = await self._parked(make_session_manager, store)  # no key was ever stamped
        assert mgr.apply_pending_compaction(_agent(live), prefix_key="m|default") is None

    @pytest.mark.asyncio
    async def test_hard_ceiling_forces_apply(self, make_session_manager):
        store = {}
        mgr, live = await self._parked(make_session_manager, store)
        await mgr.update_after_turn(1600, current_messages=live)  # pending waits post-turn...
        assert mgr.compaction_state.pending_checkpoint == 4
        assert mgr.apply_pending_compaction(_agent(live), prefix_key="m|default") == "hard_ceiling"  # ...and lands pre-call
        assert len(live) == 6

    @pytest.mark.asyncio
    async def test_live_apply_matches_a_cold_restore_of_the_same_state(self, make_session_manager):
        """The in-place slice must produce the bytes _apply_compaction derives
        from stored history under the promoted state (prefix cache parity)."""
        store = {}
        mgr, live = await self._parked(make_session_manager, store)
        stored = copy.deepcopy(live)
        _age(store, 600)
        assert mgr.apply_pending_compaction(_agent(live), prefix_key="m|default") == "cache_expired"

        cold = _manager(make_session_manager, store)
        agent = _agent(copy.deepcopy(stored))
        cold._apply_compaction(agent)
        assert _dump(agent.messages) == _dump(live)
        assert cold._live_offset == mgr._live_offset == 4

    @pytest.mark.asyncio
    async def test_pending_beyond_live_list_is_dropped_not_applied(self, make_session_manager):
        store = {}
        mgr, live = await self._parked(make_session_manager, store)
        _age(store, 600)
        short = live[:2]  # a list that does not reach the cut
        assert mgr.apply_pending_compaction(_agent(short), prefix_key="m|default") is None
        assert mgr.compaction_state.pending_checkpoint is None
        assert mgr.compaction_state.checkpoint == 0 and len(short) == 2

    @pytest.mark.asyncio
    async def test_after_apply_next_turn_rearms_and_can_cut_again(self, make_session_manager):
        store = {}
        mgr, live = await self._parked(make_session_manager, store)
        _age(store, 600)
        mgr.apply_pending_compaction(_agent(live), prefix_key="m|default")
        assert await mgr.update_after_turn(800, current_messages=live) is None
        assert mgr.compaction_state.armed is True
        live.extend(make_conversation(4))
        result = await mgr.update_after_turn(2000, current_messages=live)
        assert result is not None and result.deferred is True
        assert mgr.compaction_state.pending_checkpoint > 4  # absolute: offset 4 + relative cut

    @pytest.mark.asyncio
    async def test_apply_emits_metric_with_reason(self, make_session_manager, monkeypatch):
        import apis.shared.observability.emf as emf
        emitted = []
        monkeypatch.setattr(emf, "emit_emf_metrics", lambda ns, metrics, properties=None, units=None: emitted.append((metrics, properties)))
        monkeypatch.delenv("PROMPT_CACHE_OBSERVABILITY_ENABLED", raising=False)
        store = {}
        mgr, live = await self._parked(make_session_manager, store)
        cut_metrics = [m for m, p in emitted if "CompactionCut" in m]
        assert cut_metrics and cut_metrics[0]["CompactionDeferred"] == 1
        _age(store, 600)
        mgr.apply_pending_compaction(_agent(live), prefix_key="m|default")
        applied = [(m, p) for m, p in emitted if "CompactionApplied" in m]
        assert applied and applied[0][1]["applyReason"] == "cache_expired"
        assert applied[0][0]["CompactionAppliedForced"] == 0


class TestLedger:
    @pytest.mark.asyncio
    async def test_apply_records_an_applied_ledger_event(self, make_session_manager):
        store = {}
        mgr = _manager(make_session_manager, store)
        live = make_conversation(5)
        await mgr.update_after_turn(1200, current_messages=live)
        mgr.record_compaction_event = MagicMock()
        _age(store, 600)
        assert mgr.apply_pending_compaction(_agent(live), prefix_key="m|default") == "cache_expired"
        kinds = [c.args[0] for c in mgr.record_compaction_event.call_args_list]
        assert kinds == ["applied"]
        fields = mgr.record_compaction_event.call_args.kwargs
        assert fields["checkpoint"] == 4 and fields["retainedMessages"] == 6
        assert fields["cacheGapSeconds"] >= 599 and fields["summaryTokens"] >= 0
        assert fields["promoted"] == 1


class TestStateRoundTrip:
    def test_pending_fields_round_trip_and_default_none(self):
        assert CompactionState.from_dict({"checkpoint": 1}).pending_checkpoint is None
        s = CompactionState(pending_checkpoint=7, pending_summary="s", pending_hard_ceiling=150, pending_since="t", last_prefix_key="m|a")
        again = CompactionState.from_dict(s.to_dict())
        assert (again.pending_checkpoint, again.pending_summary, again.pending_hard_ceiling, again.pending_since, again.last_prefix_key) == (7, "s", 150, "t", "m|a")

    def test_from_env_flag(self, monkeypatch):
        monkeypatch.delenv("AGENTCORE_MEMORY_COMPACTION_DEFERRED_APPLY_ENABLED", raising=False)
        assert CompactionConfig.from_env().deferred_apply_enabled is True
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_DEFERRED_APPLY_ENABLED", "false")
        assert CompactionConfig.from_env().deferred_apply_enabled is False
