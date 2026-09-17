"""Bounded compaction summary — spiral spec PR-2 / thresholds spec §3.6."""

import sys
import types
from unittest.mock import MagicMock

import pytest

from agents.main_agent.session.compaction_models import CompactionConfig, CompactionState
from agents.main_agent.session.compaction_summary import (
    approx_tokens,
    bound_summary,
    truncate_records_newest_first,
)

from .conftest import make_conversation


BUDGET = 100  # tokens → 400 chars


@pytest.fixture
def bedrock(monkeypatch):
    """Patch boto3 so no test reaches Bedrock; returns the converse mock."""
    converse = MagicMock()
    client = MagicMock()
    client.converse = converse
    module = types.SimpleNamespace(client=MagicMock(return_value=client))
    monkeypatch.setitem(sys.modules, "boto3", module)
    return converse


def _model_reply(text, stop="end_turn"):
    return {"stopReason": stop, "output": {"message": {"content": [{"text": text}]}}}


class TestTruncateNewestFirst:
    def test_keeps_newest_records_that_fit(self):
        records = ["old " * 50, "mid " * 50, "new " * 50]  # 200 chars each
        out = truncate_records_newest_first(records, budget_tokens=110)  # 440 chars
        assert out.startswith("mid") and out.endswith("new ")
        assert "old" not in out

    def test_keeps_tail_of_newest_when_nothing_fits(self):
        newest = "x" * 1000 + "TAIL"
        out = truncate_records_newest_first(["ancient", newest], budget_tokens=10)  # 40 chars
        assert out.endswith("TAIL") and len(out) == 40

    def test_empty(self):
        assert truncate_records_newest_first([], 10) is None
        assert truncate_records_newest_first(["", ""], 10) is None


class TestBoundSummary:
    @pytest.mark.asyncio
    async def test_within_budget_is_untouched(self, bedrock):
        result = await bound_summary(["a", "b"], BUDGET, model_enabled=True, model_id="m")
        assert result.text == "a\n\nb" and result.outcome == "within_budget"
        bedrock.assert_not_called()

    @pytest.mark.asyncio
    async def test_model_compresses_once_at_cut(self, bedrock):
        bedrock.return_value = _model_reply("Standing instructions: cite APA. Open: intro draft.")
        records = ["r" * 300, "s" * 300]
        result = await bound_summary(records, BUDGET, model_enabled=True, model_id="m")
        assert result.outcome == "model"
        assert approx_tokens(result.text) <= BUDGET
        assert bedrock.call_count == 1
        kwargs = bedrock.call_args.kwargs
        assert kwargs["modelId"] == "m"
        assert "Standing instructions" in kwargs["system"][0]["text"]

    @pytest.mark.asyncio
    async def test_model_failure_falls_back_to_newest_first(self, bedrock):
        bedrock.side_effect = RuntimeError("throttled")
        records = ["old " * 100, "new " * 50]  # 400 + 200 chars
        result = await bound_summary(records, BUDGET, model_enabled=True, model_id="m")
        assert result.outcome == "truncated_after_model"
        assert result.text.startswith("new") and "old" not in result.text
        assert approx_tokens(result.text) <= BUDGET

    @pytest.mark.asyncio
    async def test_model_ceiling_hit_is_a_failure(self, bedrock):
        bedrock.return_value = _model_reply("frag", stop="max_tokens")
        result = await bound_summary(["r" * 900], BUDGET, model_enabled=True, model_id="m")
        assert result.outcome == "truncated_after_model"
        assert approx_tokens(result.text) <= BUDGET

    @pytest.mark.asyncio
    async def test_model_overshoot_is_tail_trimmed(self, bedrock):
        bedrock.return_value = _model_reply("y" * 2000 + "END")
        result = await bound_summary(["r" * 900], BUDGET, model_enabled=True, model_id="m")
        assert result.outcome == "truncated_after_model"
        assert result.text.endswith("END") and approx_tokens(result.text) <= BUDGET

    @pytest.mark.asyncio
    async def test_kill_switch_skips_model(self, bedrock):
        result = await bound_summary(["r" * 900], BUDGET, model_enabled=False, model_id="m")
        assert result.outcome == "truncated"
        bedrock.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty(self, bedrock):
        result = await bound_summary([], BUDGET, model_enabled=True, model_id="m")
        assert result.text is None and result.outcome == "empty"

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_SUMMARY_TOKEN_BUDGET", "1234")
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_SUMMARY_MODEL_ENABLED", "false")
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_SUMMARY_MODEL_ID", "us.amazon.nova-lite-v1:0")
        cfg = CompactionConfig.from_env()
        assert cfg.summary_token_budget == 1234
        assert cfg.summary_model_enabled is False
        assert cfg.summary_model_id == "us.amazon.nova-lite-v1:0"
        monkeypatch.delenv("AGENTCORE_MEMORY_COMPACTION_SUMMARY_MODEL_ENABLED")
        assert CompactionConfig.from_env().summary_model_enabled is True


class TestThroughUpdateAfterTurn:
    """Acceptance: oversized LTM records → the persisted summary is ≤ budget and
    the restore prepends the same bounded bytes."""

    def _manager(self, make_session_manager, records, **cfg):
        config = CompactionConfig(enabled=True, deferred_apply_enabled=False, token_threshold=1000, protected_turns=3,
                                  summary_token_budget=BUDGET, **cfg)
        mgr = make_session_manager(compaction_config=config)
        mgr.compaction_state = CompactionState()
        mgr._save_compaction_state = MagicMock()
        mgr._retrieve_session_summaries = MagicMock(return_value=records)
        mgr._valid_cutoff_indices = [0, 2, 4, 6, 8]
        mgr._all_messages_for_summary = make_conversation(5)
        return mgr

    @pytest.mark.asyncio
    async def test_oversized_ltm_join_is_bounded_and_persisted(self, make_session_manager, bedrock):
        bedrock.side_effect = RuntimeError("no model in tests")
        records = [f"record {i} " + "z" * 600 for i in range(10)]  # ~6k chars
        mgr = self._manager(make_session_manager, records)
        result = await mgr.update_after_turn(2000)
        assert result is not None
        state = mgr.compaction_state
        assert approx_tokens(state.summary) <= BUDGET
        # Nothing fits whole (each record ~610 chars vs a 400-char budget), so
        # the tail of the NEWEST record is kept — never the oldest.
        assert state.summary == records[-1][-BUDGET * 4:]
        assert state.policy["summarySource"] == "ltm"
        assert state.policy["summaryOutcome"] == "truncated_after_model"
        assert state.policy["summaryTokensBefore"] > BUDGET >= state.policy["summaryTokensAfter"]
        assert state.policy["summaryTokenBudget"] == BUDGET
        # The restore path prepends exactly the persisted bytes.
        restored = mgr._prepend_summary_to_first_message(make_conversation(2), state.summary)
        assert state.summary in restored[0]["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_small_ltm_join_is_untouched(self, make_session_manager, bedrock):
        mgr = self._manager(make_session_manager, ["LTM summary 1", "LTM summary 2"])
        await mgr.update_after_turn(2000)
        assert mgr.compaction_state.summary == "LTM summary 1\n\nLTM summary 2"
        assert mgr.compaction_state.policy["summaryOutcome"] == "within_budget"
        bedrock.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_summary_is_labelled(self, make_session_manager, bedrock):
        mgr = self._manager(make_session_manager, [])
        await mgr.update_after_turn(2000)
        assert mgr.compaction_state.policy["summarySource"] == "fallback"
        assert "Previous conversation" in mgr.compaction_state.summary


class TestCompactionMetrics:
    @pytest.mark.asyncio
    async def test_cut_emits_one_content_free_emf_record(self, make_session_manager, bedrock, monkeypatch):
        import apis.shared.observability.emf as emf
        emitted = []
        monkeypatch.setattr(emf, "emit_emf_metrics", lambda ns, metrics, properties=None, units=None: emitted.append((ns, metrics, properties)))
        monkeypatch.delenv("PROMPT_CACHE_OBSERVABILITY_ENABLED", raising=False)
        config = CompactionConfig(enabled=True, deferred_apply_enabled=False, token_threshold=1000, protected_turns=3, summary_token_budget=BUDGET)
        mgr = make_session_manager(compaction_config=config)
        mgr.compaction_state = CompactionState()
        mgr._save_compaction_state = MagicMock()
        mgr._retrieve_session_summaries = MagicMock(return_value=["tiny"])
        mgr._valid_cutoff_indices = [0, 2, 4, 6, 8]
        mgr._all_messages_for_summary = make_conversation(5)

        await mgr.update_after_turn(2000)

        assert len(emitted) == 1
        ns, metrics, props = emitted[0]
        assert ns == "AgentCoreStack/Compaction"
        assert metrics["CompactionCut"] == 1 and metrics["CompactionForced"] == 0
        assert metrics["CompactionInputTokens"] == 2000
        # Tiny 5-turn conversation calibrated to 2000 tokens against a 250
        # floor with 3 protected turns: the tail cannot fit → unreachable.
        assert metrics["CompactionFloorUnreachable"] == 1
        assert props["summaryOutcome"] == "within_budget" and props["policySource"] == "fixed"
        # Content-free: no summary text, no message text in the record.
        assert "tiny" not in str(props)

    @pytest.mark.asyncio
    async def test_kill_switch_silences_metrics(self, make_session_manager, bedrock, monkeypatch):
        import apis.shared.observability.emf as emf
        emitted = []
        monkeypatch.setattr(emf, "emit_emf_metrics", lambda *a, **k: emitted.append(a))
        monkeypatch.setenv("PROMPT_CACHE_OBSERVABILITY_ENABLED", "false")
        config = CompactionConfig(enabled=True, deferred_apply_enabled=False, token_threshold=1000, protected_turns=3)
        mgr = make_session_manager(compaction_config=config)
        mgr.compaction_state = CompactionState()
        mgr._save_compaction_state = MagicMock()
        mgr._retrieve_session_summaries = MagicMock(return_value=[])
        mgr._valid_cutoff_indices = [0, 2, 4, 6, 8]
        mgr._all_messages_for_summary = make_conversation(5)
        await mgr.update_after_turn(2000)
        assert emitted == []
