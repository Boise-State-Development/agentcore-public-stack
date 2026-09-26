"""Bounded compaction summary — spiral spec PR-2 / thresholds spec §3.6."""

import sys
import types
from unittest.mock import MagicMock

import pytest

from agents.main_agent.session.compaction_models import CompactionConfig, CompactionState
from agents.main_agent.session.compaction_summary import (
    approx_tokens,
    bound_summary,
    compress_with_model,
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
    async def test_model_ceiling_hit_with_no_complete_line_falls_back(self, bedrock):
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

    def test_default_model_is_nova_2_lite(self, monkeypatch):
        # Nova Micro dropped ~42% of exact identifiers on the quality harness;
        # a revert to it should be a deliberate change, not a drive-by.
        monkeypatch.delenv("AGENTCORE_MEMORY_COMPACTION_SUMMARY_MODEL_ID", raising=False)
        assert CompactionConfig.from_env().summary_model_id == "us.amazon.nova-2-lite-v1:0"
        assert CompactionConfig().summary_model_id == "us.amazon.nova-2-lite-v1:0"


class TestCeilingSalvage:
    """A generation cut off by ``maxTokens`` keeps its complete lines.

    Nova 2 Lite's narrative length varies several times over on the same
    records, so hitting the ceiling is routine. Discarding the generation
    fell back to newest-first truncation of the raw records, which drops the
    oldest standing instructions first.
    """

    CUT_OFF = "Standing instructions: cite APA.\nDecisions: title is Tides.\nOpen: the conclu"

    @pytest.mark.asyncio
    async def test_keeps_complete_lines_and_drops_the_partial_one(self, bedrock):
        bedrock.return_value = _model_reply(self.CUT_OFF, stop="max_tokens")
        result = await bound_summary(["r" * 900], BUDGET, model_enabled=True, model_id="m")
        assert result.outcome == "model_salvaged"
        assert result.text == "Standing instructions: cite APA.\nDecisions: title is Tides."
        assert result.tokens_after == approx_tokens(result.text)

    @pytest.mark.asyncio
    async def test_over_budget_salvage_keeps_the_head_not_the_tail(self, bedrock):
        # 10 lines of 60 chars against a 400-char budget: the standing
        # instructions at the head survive, the cut-off end does not.
        lines = ["INSTRUCTIONS: never contact the vendor directly".ljust(60)]
        lines += [f"detail {i}".ljust(60) for i in range(9)]
        bedrock.return_value = _model_reply("\n".join(lines) + "\npartial", stop="max_tokens")
        result = await bound_summary(["r" * 900], BUDGET, model_enabled=True, model_id="m")
        assert result.outcome == "model_salvaged"
        assert result.text.startswith("INSTRUCTIONS: never contact the vendor directly")
        assert "partial" not in result.text
        assert approx_tokens(result.text) <= BUDGET
        # Whole lines only: the text ends where one of the model's lines ends.
        assert result.text.split("\n")[-1] == lines[len(result.text.split("\n")) - 1].rstrip()

    @pytest.mark.asyncio
    async def test_compress_with_model_returns_the_salvage(self, bedrock):
        # The public helper other cut paths call gets the same salvage.
        bedrock.return_value = _model_reply(self.CUT_OFF, stop="max_tokens")
        out = await compress_with_model(["r" * 900], BUDGET, model_id="m")
        assert out == "Standing instructions: cite APA.\nDecisions: title is Tides."

    @pytest.mark.asyncio
    async def test_complete_generation_is_still_plain_model(self, bedrock):
        bedrock.return_value = _model_reply(self.CUT_OFF)
        result = await bound_summary(["r" * 900], BUDGET, model_enabled=True, model_id="m")
        assert result.outcome == "model" and result.text == self.CUT_OFF

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "model_id, expected",
        [
            # Nova 2 Lite's card: 64K max output, so the budget binds.
            ("us.amazon.nova-2-lite-v1:0", 8_000),
            ("global.amazon.nova-2-lite-v1:0", 8_000),
            ("amazon.nova-2-lite-v1:0", 8_000),
            # Anything unlisted stays under Nova Micro's 5K ceiling.
            ("us.amazon.nova-micro-v1:0", 4_000),
            ("us.anthropic.claude-haiku-4-5-20251001-v1:0", 4_000),
        ],
    )
    async def test_max_tokens_follows_the_models_ceiling(self, bedrock, model_id, expected):
        bedrock.return_value = _model_reply("ok")
        await bound_summary(["r" * 40_000], 8_000, model_enabled=True, model_id=model_id)
        assert bedrock.call_args.kwargs["inferenceConfig"]["maxTokens"] == expected

    @pytest.mark.asyncio
    async def test_budget_below_the_ceiling_still_binds(self, bedrock):
        bedrock.return_value = _model_reply("ok")
        await bound_summary(["r" * 40_000], 3_000, model_enabled=True, model_id="us.amazon.nova-2-lite-v1:0")
        assert bedrock.call_args.kwargs["inferenceConfig"]["maxTokens"] == 3_000


CLAUDE_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def _reject_temperature_with_top_p(**kwargs):
    """Bedrock's behavior for Claude 4.5+: both sampling params is a ValidationException."""
    config = kwargs.get("inferenceConfig", {})
    if "temperature" in config and "topP" in config:
        raise RuntimeError(
            "ValidationException: `temperature` and `top_p` cannot both be specified for this model."
        )
    return _model_reply("Standing instructions: cite APA. Open: intro draft.")


class TestClaudeSummaryModel:
    """A Claude ``summary_model_id`` must compress, not silently truncate.

    Sending ``temperature`` with ``topP`` made every compression fail against
    Claude 4.5+ and fall back to truncation, with nothing but a warning log.
    """

    @pytest.mark.asyncio
    async def test_inference_config_never_carries_both_sampling_params(self, bedrock):
        bedrock.return_value = _model_reply("ok")
        await bound_summary(["r" * 900], BUDGET, model_enabled=True, model_id=CLAUDE_MODEL_ID)
        config = bedrock.call_args.kwargs["inferenceConfig"]
        assert not ("temperature" in config and "topP" in config)

    @pytest.mark.asyncio
    async def test_claude_model_id_returns_the_model_text(self, bedrock):
        bedrock.side_effect = _reject_temperature_with_top_p
        result = await compress_with_model(["r" * 900], BUDGET, model_id=CLAUDE_MODEL_ID)
        assert result == "Standing instructions: cite APA. Open: intro draft."
        bounded = await bound_summary(["r" * 900], BUDGET, model_enabled=True, model_id=CLAUDE_MODEL_ID)
        assert bounded.outcome == "model"
        assert bedrock.call_args.kwargs["modelId"] == CLAUDE_MODEL_ID


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
    async def test_salvaged_summary_is_persisted_and_labelled(self, make_session_manager, bedrock):
        bedrock.return_value = _model_reply("Instructions: cite APA.\nOpen: the conclu", stop="max_tokens")
        records = [f"record {i} " + "z" * 600 for i in range(10)]
        mgr = self._manager(make_session_manager, records)
        await mgr.update_after_turn(2000)
        state = mgr.compaction_state
        assert state.summary == "Instructions: cite APA."
        assert state.policy["summaryOutcome"] == "model_salvaged"
        assert bedrock.call_count == 1

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
    async def test_cut_record_carries_the_session_id_as_a_property_not_a_dimension(
        self, make_session_manager, bedrock, monkeypatch
    ):
        """The readout had to join cuts to sessions by matching input tokens
        within ±30 min. The session id now rides the record — as a queryable
        property, never a dimension (one metric stream per conversation)."""
        import json

        import apis.shared.observability.emf as emf

        lines = []
        monkeypatch.setattr(emf._emf_logger, "info", lambda line: lines.append(line))
        monkeypatch.delenv("PROMPT_CACHE_OBSERVABILITY_ENABLED", raising=False)
        config = CompactionConfig(enabled=True, deferred_apply_enabled=False, token_threshold=1000, protected_turns=3, summary_token_budget=BUDGET)
        mgr = make_session_manager(compaction_config=config)
        mgr.compaction_state = CompactionState()
        mgr._save_compaction_state = MagicMock()
        mgr._retrieve_session_summaries = MagicMock(return_value=["a summary that must not leak"])
        mgr._valid_cutoff_indices = [0, 2, 4, 6, 8]
        mgr._all_messages_for_summary = make_conversation(5)

        await mgr.update_after_turn(2000)

        records = [json.loads(line) for line in lines]
        cut = [r for r in records if r.get("CompactionCut") == 1]
        assert len(cut) == 1
        record = cut[0]
        assert record["sessionId"] == mgr.config.session_id
        directive = record["_aws"]["CloudWatchMetrics"][0]
        assert directive["Namespace"] == "AgentCoreStack/Compaction"
        assert directive["Dimensions"] == [[]]
        assert "sessionId" not in {m["Name"] for m in directive["Metrics"]}
        # Content-free: neither the summary nor any message text is emitted.
        raw = "".join(lines)
        assert "must not leak" not in raw
        assert "Question" not in raw and "Answer" not in raw

    @pytest.mark.asyncio
    async def test_every_compaction_record_carries_the_session_id(self, make_session_manager, monkeypatch):
        import apis.shared.observability.emf as emf

        emitted = []
        monkeypatch.setattr(emf, "emit_emf_metrics", lambda ns, metrics, properties=None, units=None: emitted.append(properties))
        monkeypatch.delenv("PROMPT_CACHE_OBSERVABILITY_ENABLED", raising=False)
        mgr = make_session_manager(compaction_config=CompactionConfig(enabled=True))
        mgr._emit_emf({"CompactionApplied": 1}, {"applyReason": "cache_expired"})
        mgr._emit_emf({"TruncationAnchorAdvanced": 1}, {"anchorFrom": 0, "anchorTo": 8})
        assert [p["sessionId"] for p in emitted] == [mgr.config.session_id] * 2
        assert emitted[0]["applyReason"] == "cache_expired"

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
