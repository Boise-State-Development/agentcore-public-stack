"""Model-relative compaction policy — docs/specs/compaction-model-relative-thresholds.md.

Pins the §3.1 table, the kill switch, the floor-seeking cut (§3.2) and the
hysteresis rule (§3.3) as exercised through ``update_after_turn``.
"""

from unittest.mock import MagicMock

import pytest

from agents.main_agent.session.compaction_models import CompactionConfig, CompactionState
from agents.main_agent.session.compaction_policy import (
    IMAGE_TOKEN_ESTIMATE,
    CompactionPolicy,
    choose_checkpoint,
    estimate_message_tokens,
)

from .conftest import make_assistant_message, make_conversation, make_user_message


# ---------------------------------------------------------------------------
# §3.1 — the table
# ---------------------------------------------------------------------------

class TestPolicyResolution:
    @pytest.mark.parametrize(
        "window, ceiling, floor, hard",
        [
            (128_000, 64_000, 16_000, 89_600),
            (200_000, 100_000, 25_000, 140_000),
            (256_000, 100_000, 25_000, 150_000),
            (272_000, 100_000, 25_000, 150_000),
            (1_000_000, 100_000, 25_000, 150_000),
        ],
    )
    def test_table_rows(self, window, ceiling, floor, hard):
        policy = CompactionPolicy.resolve(CompactionConfig(), window)
        assert (policy.ceiling, policy.floor, policy.hard_ceiling) == (ceiling, floor, hard)
        assert policy.source == "model_relative"
        assert policy.context_window == window

    def test_unknown_window_uses_fixed_threshold(self):
        policy = CompactionPolicy.resolve(CompactionConfig(), None)
        assert policy.source == "fixed"
        assert policy.ceiling == 100_000
        assert policy.floor == 25_000
        assert policy.hard_ceiling == 150_000
        assert policy.hysteresis_enabled

    @pytest.mark.parametrize("bad", [0, -5, "abc"])
    def test_invalid_window_treated_as_unknown(self, bad):
        assert CompactionPolicy.resolve(CompactionConfig(), bad).source == "fixed"

    def test_kill_switch_is_legacy(self):
        policy = CompactionPolicy.resolve(
            CompactionConfig(model_relative_enabled=False, token_threshold=1_000), 1_000_000
        )
        assert policy.source == "legacy"
        assert policy.ceiling == 1_000
        assert policy.floor is None
        assert policy.hard_ceiling is None
        assert not policy.hysteresis_enabled

    def test_hard_never_below_ceiling_and_floor_always_below(self):
        cfg = CompactionConfig(hard_ceiling_ratio=0.1, floor_ratio=2.0)
        policy = CompactionPolicy.resolve(cfg, 200_000)
        assert policy.hard_ceiling >= policy.ceiling
        assert policy.floor < policy.ceiling

    def test_from_env_reads_policy_fields(self, monkeypatch):
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_MODEL_RELATIVE_ENABLED", "false")
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_CEILING_RATIO", "0.4")
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_CEILING_CAP_TOKENS", "150000")
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_FLOOR_RATIO", "0.2")
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_HARD_CEILING_RATIO", "0.6")
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_HARD_CEILING_MULTIPLIER", "1.2")
        cfg = CompactionConfig.from_env()
        assert cfg.model_relative_enabled is False
        assert cfg.ceiling_ratio == 0.4
        assert cfg.ceiling_cap_tokens == 150_000
        assert cfg.floor_ratio == 0.2
        assert cfg.hard_ceiling_ratio == 0.6
        assert cfg.hard_ceiling_multiplier == 1.2

    def test_kill_switch_default_on_and_only_literal_false_disables(self, monkeypatch):
        monkeypatch.delenv("AGENTCORE_MEMORY_COMPACTION_MODEL_RELATIVE_ENABLED", raising=False)
        assert CompactionConfig.from_env().model_relative_enabled is True
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_MODEL_RELATIVE_ENABLED", "")
        assert CompactionConfig.from_env().model_relative_enabled is True
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_MODEL_RELATIVE_ENABLED", "FALSE")
        assert CompactionConfig.from_env().model_relative_enabled is False


# ---------------------------------------------------------------------------
# Estimator
# ---------------------------------------------------------------------------

class TestEstimator:
    def test_text_scales_with_length(self):
        short = estimate_message_tokens(make_user_message("a" * 40))
        long = estimate_message_tokens(make_user_message("a" * 4000))
        assert long > short * 10

    def test_image_is_flat(self):
        msg = {"role": "user", "content": [{"image": {"format": "png", "source": {"bytes": b"x" * 10}}}]}
        assert estimate_message_tokens(msg) >= IMAGE_TOKEN_ESTIMATE

    def test_tool_result_counts_inner_text(self):
        msg = {
            "role": "user",
            "content": [{"toolResult": {"toolUseId": "t1", "content": [{"text": "r" * 4000}]}}],
        }
        assert estimate_message_tokens(msg) >= 1000

    @pytest.mark.parametrize("weird", [None, "plain", {"role": "user"}, {"role": "user", "content": "str"}, {"content": [None, 3]}])
    def test_never_raises(self, weird):
        assert estimate_message_tokens(weird) >= 0


# ---------------------------------------------------------------------------
# §3.2 — floor-seeking cut
# ---------------------------------------------------------------------------

def _conversation_with_sizes(sizes):
    """user/assistant pairs where each user message carries `size` chars."""
    messages = []
    for i, size in enumerate(sizes):
        messages.append(make_user_message("u" * size))
        messages.append(make_assistant_message(f"a{i}"))
    return messages


class TestChooseCheckpoint:
    def test_keeps_minimum_protected_turns(self):
        msgs = make_conversation(3)
        assert choose_checkpoint(msgs, [0, 2, 4], protected_turns=3, floor_tokens=10, history_tokens=None) == (0, None)

    def test_picks_oldest_cut_that_fits_under_floor(self):
        # 6 turns; the last three are tiny, turns 2-3 are big. Floor admits the
        # tail plus turn 3 but not turn 2 — oldest fitting cut is turn 3 (index 4).
        msgs = _conversation_with_sizes([4000, 4000, 4000, 40, 40, 40])
        cutoffs = [0, 2, 4, 6, 8, 10]
        # raw: big user ~1000 tok + overhead, small ~10+overhead; calibrate to 3500 total
        cut, retained = choose_checkpoint(msgs, cutoffs, 3, floor_tokens=1400, history_tokens=3500)
        assert cut == 4
        assert retained is not None and retained <= 1400

    def test_falls_back_to_minimum_protection_when_tail_too_big(self):
        msgs = _conversation_with_sizes([40, 40, 40, 8000, 8000, 8000])
        cutoffs = [0, 2, 4, 6, 8, 10]
        cut, retained = choose_checkpoint(msgs, cutoffs, 3, floor_tokens=100, history_tokens=6000)
        assert cut == 6  # cutoffs[-3]
        assert retained > 100

    def test_no_cut_when_everything_fits(self):
        msgs = _conversation_with_sizes([40] * 6)
        cut, _ = choose_checkpoint(msgs, [0, 2, 4, 6, 8, 10], 3, floor_tokens=10_000, history_tokens=200)
        assert cut == 0

    def test_calibration_scales_estimates_to_history_tokens(self):
        msgs = _conversation_with_sizes([400] * 6)  # equal turns
        cutoffs = [0, 2, 4, 6, 8, 10]
        # History measured at 6000 tokens → each turn ~1000. Floor 2500 admits
        # only the two newest turns... but protection keeps three → min-protection.
        cut, retained = choose_checkpoint(msgs, cutoffs, 3, floor_tokens=2500, history_tokens=6000)
        assert cut == 6
        # Floor 3500 admits exactly three turns → cut at 6 again (oldest fitting)
        cut2, _ = choose_checkpoint(msgs, cutoffs, 3, floor_tokens=3500, history_tokens=6000)
        assert cut2 == 6
        # Floor 4500 admits four turns → cut at 4
        cut3, _ = choose_checkpoint(msgs, cutoffs, 3, floor_tokens=4500, history_tokens=6000)
        assert cut3 == 4

    def test_empty_messages_uses_minimum_protection(self):
        assert choose_checkpoint([], [0, 2, 4, 6], 3, 10, 100)[0] == 2


# ---------------------------------------------------------------------------
# §3.3 / §3.4 — hysteresis and coordinates through update_after_turn
# ---------------------------------------------------------------------------

def _armed_manager(make_session_manager, compaction_config, checkpoint=0, armed=True):
    mgr = make_session_manager(compaction_config=compaction_config)
    mgr.compaction_state = CompactionState(checkpoint=checkpoint, armed=armed)
    mgr._save_compaction_state = MagicMock()
    mgr._retrieve_session_summaries = MagicMock(return_value=[])
    mgr._valid_cutoff_indices = [0, 2, 4, 6, 8]
    mgr._all_messages_for_summary = make_conversation(5)
    return mgr


class TestHysteresis:
    """compaction_config fixture: threshold 1000 → fixed policy ceiling 1000,
    floor 250, hard 1500 (window unknown)."""

    @pytest.mark.asyncio
    async def test_cut_disarms(self, make_session_manager, compaction_config):
        mgr = _armed_manager(make_session_manager, compaction_config)
        result = await mgr.update_after_turn(1200)
        assert result is not None
        assert mgr.compaction_state.armed is False
        assert result.forced is False
        assert result.ceiling == 1000 and result.floor == 250 and result.hard_ceiling == 1500
        assert mgr.compaction_state.policy["source"] == "fixed"

    @pytest.mark.asyncio
    async def test_over_ceiling_while_disarmed_is_a_noop(self, make_session_manager, compaction_config):
        mgr = _armed_manager(make_session_manager, compaction_config, checkpoint=4, armed=False)
        mgr._valid_cutoff_indices = [0, 2, 4, 6, 8, 10]
        mgr._all_messages_for_summary = make_conversation(6)
        result = await mgr.update_after_turn(1200)
        assert result is None
        assert mgr.compaction_state.checkpoint == 4
        assert mgr.compaction_state.armed is False
        mgr._save_compaction_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_hard_ceiling_forces_a_cut_while_disarmed(self, make_session_manager, compaction_config):
        mgr = _armed_manager(make_session_manager, compaction_config, checkpoint=4, armed=False)
        mgr._valid_cutoff_indices = [0, 2, 4, 6, 8, 10]
        mgr._all_messages_for_summary = make_conversation(6)
        result = await mgr.update_after_turn(1500)
        assert result is not None and result.forced is True
        assert mgr.compaction_state.checkpoint == 6
        assert mgr.compaction_state.policy["forced"] is True

    @pytest.mark.asyncio
    async def test_armed_cut_above_hard_ceiling_is_not_forced(self, make_session_manager, compaction_config):
        """Forced means "ran while disarmed" — the spiral signal — not "was large"."""
        mgr = _armed_manager(make_session_manager, compaction_config)
        result = await mgr.update_after_turn(2500)  # above hard=1500, but armed
        assert result is not None and result.forced is False
        assert mgr.compaction_state.policy["forced"] is False

    @pytest.mark.asyncio
    async def test_under_ceiling_rearms(self, make_session_manager, compaction_config):
        mgr = _armed_manager(make_session_manager, compaction_config, checkpoint=4, armed=False)
        assert await mgr.update_after_turn(900) is None
        assert mgr.compaction_state.armed is True

    @pytest.mark.asyncio
    async def test_spiral_shape_cuts_exactly_once(self, make_session_manager, compaction_config):
        """Input pinned above the ceiling for 10 turns: one cut, then no-ops."""
        mgr = _armed_manager(make_session_manager, compaction_config)
        advances = 0
        for turn in range(10):
            n_turns = 5 + turn
            mgr._valid_cutoff_indices = list(range(0, 2 * n_turns, 2))
            mgr._all_messages_for_summary = make_conversation(n_turns)
            if await mgr.update_after_turn(1200) is not None:
                advances += 1
        assert advances == 1

    @pytest.mark.asyncio
    async def test_legacy_mode_never_disarms_and_uses_turn_count(self, make_session_manager):
        cfg = CompactionConfig(enabled=True, deferred_apply_enabled=False, token_threshold=1000, protected_turns=3, model_relative_enabled=False)
        mgr = _armed_manager(make_session_manager, cfg)
        first = await mgr.update_after_turn(1200)
        assert first is not None and first.floor is None
        assert mgr.compaction_state.checkpoint == 4  # cutoffs[-3]
        assert mgr.compaction_state.armed is True
        mgr._valid_cutoff_indices = [0, 2, 4, 6, 8, 10]
        mgr._all_messages_for_summary = make_conversation(6)
        second = await mgr.update_after_turn(1200)
        assert second is not None and mgr.compaction_state.checkpoint == 6


class TestCoordinates:
    @pytest.mark.asyncio
    async def test_checkpoint_is_live_offset_plus_relative_cut(self, make_session_manager, compaction_config):
        mgr = _armed_manager(make_session_manager, compaction_config, checkpoint=10)
        mgr._live_offset = 10  # restore sliced at absolute 10
        result = await mgr.update_after_turn(1200)
        assert result is not None
        assert result.previous_checkpoint == 10
        assert result.new_checkpoint == 14  # 10 + cutoffs[-3]=4
        assert mgr.compaction_state.truncation_anchor == 14

    @pytest.mark.asyncio
    async def test_context_window_flows_into_policy(self, make_session_manager):
        cfg = CompactionConfig(enabled=True, deferred_apply_enabled=False, protected_turns=3)
        mgr = _armed_manager(make_session_manager, cfg)
        # 128k window → ceiling 64k; 60k is under it → no cut.
        assert await mgr.update_after_turn(60_000, context_window=128_000) is None
        assert mgr.compaction_state.checkpoint == 0
        # 1M window → ceiling capped at 100k (not 500k) → 150k cuts.
        result = await mgr.update_after_turn(150_000, context_window=1_000_000)
        assert result is not None and result.context_window == 1_000_000 and result.ceiling == 100_000
        assert result.hard_ceiling == 150_000


class TestCompactionLedgerEvents:
    """Decisions are handed to the per-call compaction ledger when it exists
    (cost-diagnostics ``record_compaction_event``), and are a no-op otherwise."""

    @pytest.mark.asyncio
    async def test_forced_and_floor_unreachable_are_recorded(self, make_session_manager, compaction_config):
        mgr = _armed_manager(make_session_manager, compaction_config, checkpoint=4, armed=False)
        mgr._valid_cutoff_indices = [0, 2, 4, 6, 8, 10]
        mgr._all_messages_for_summary = make_conversation(6)
        mgr.record_compaction_event = MagicMock()
        result = await mgr.update_after_turn(1500)  # disarmed + hard ceiling → forced
        assert result is not None and result.forced
        kinds = [c.args[0] for c in mgr.record_compaction_event.call_args_list]
        assert "forced" in kinds and "floor_unreachable" in kinds
        forced_call = next(c for c in mgr.record_compaction_event.call_args_list if c.args[0] == "forced")
        assert forced_call.kwargs == {"checkpoint": 6, "inputTokens": 1500}
        floor_call = next(c for c in mgr.record_compaction_event.call_args_list if c.args[0] == "floor_unreachable")
        assert floor_call.kwargs["retainedTokens"] > 250

    @pytest.mark.asyncio
    async def test_no_ledger_is_a_noop(self, make_session_manager, compaction_config, monkeypatch):
        # The cost-diagnostics ledger now ships on this class, so absence has to
        # be simulated: strip the recorder and prove the cut still lands rather
        # than raising through ``_record_ledger_event``'s getattr seam.
        mgr = _armed_manager(make_session_manager, compaction_config)
        monkeypatch.delattr(type(mgr), "record_compaction_event")
        assert not hasattr(mgr, "record_compaction_event")
        assert await mgr.update_after_turn(1200) is not None  # no AttributeError
