"""Tests for the offline compaction quality harness (scripts/compaction_quality)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from compaction_quality.arms import default_arms, history_tokens, plant_availability, simulate  # noqa: E402
from compaction_quality.ask import build_request, estimate_cost  # noqa: E402
from compaction_quality.corpus import CorpusConfig, build_transcript, message_text  # noqa: E402
from compaction_quality.score import compare, contains_any, is_correct, mcnemar_exact, task_outcomes  # noqa: E402

SMALL = CorpusConfig(turns=30)


@pytest.fixture(scope="module")
def transcript():
    return build_transcript(0, SMALL)


@pytest.fixture(scope="module")
def arms():
    return default_arms()


class TestCorpus:
    def test_deterministic(self):
        assert build_transcript(3, SMALL).turns == build_transcript(3, SMALL).turns

    def test_variants_differ(self):
        assert build_transcript(0, SMALL).turns != build_transcript(1, SMALL).turns

    def test_every_plant_is_stated_at_its_turn(self, transcript):
        for plant in transcript.plants:
            text = "\n".join(message_text(m) for m in transcript.turns[plant.turn])
            assert contains_any(text, plant.expected), plant.plant_id
            if plant.first_turn is not None:
                first = "\n".join(message_text(m) for m in transcript.turns[plant.first_turn])
                assert contains_any(first, plant.forbidden)

    def test_no_two_statements_share_a_turn(self, transcript):
        turns = [p.turn for p in transcript.plants] + [p.first_turn for p in transcript.plants if p.first_turn is not None]
        assert len(turns) == len(set(turns))

    def test_tool_pairs_are_complete(self, transcript):
        for turn in transcript.turns:
            uses = [b["toolUse"]["toolUseId"] for m in turn for b in m["content"] if "toolUse" in b]
            results = [b["toolResult"]["toolUseId"] for m in turn for b in m["content"] if "toolResult" in b]
            assert uses == results

    def test_turns_alternate_roles(self, transcript):
        roles = [m["role"] for m in transcript.messages]
        assert all(a != b for a, b in zip(roles, roles[1:]))


class TestArms:
    def test_full_arm_is_the_untouched_history(self, transcript, arms):
        run = simulate(transcript, arms["full"])
        assert run.history == transcript.messages
        assert run.cuts == 0

    def test_model_relative_cuts_and_lands_under_the_ceiling(self, transcript, arms):
        run = simulate(transcript, arms["model_relative"], pace="cold", overhead_tokens=40_000)
        assert run.cuts >= 1
        assert run.probe_input_tokens < run.policy["ceiling"]
        assert run.history[0]["role"] == "user"
        assert "<conversation_summary>" in run.history[0]["content"][0]["text"]

    def test_model_relative_keeps_at_least_what_legacy_keeps(self, transcript, arms):
        # Both cut at the same ceiling; the floor-seeking cut never keeps fewer
        # turns than legacy's "last protected_turns".
        relative = simulate(transcript, arms["model_relative"], overhead_tokens=40_000)
        legacy = simulate(transcript, arms["legacy"], overhead_tokens=40_000)
        assert relative.live_offset <= legacy.live_offset

    def test_warm_pace_defers_the_cut_until_the_hard_ceiling(self, transcript, arms):
        warm = simulate(transcript, arms["model_relative"], pace="warm", overhead_tokens=40_000)
        cold = simulate(transcript, arms["model_relative"], pace="cold", overhead_tokens=40_000)
        assert cold.policy["lastCut"]["applied"] == "cache_expired"
        assert warm.policy["lastCut"].get("applied") in (None, "hard_ceiling")
        assert warm.live_offset <= cold.live_offset
        assert not any(e["kind"] == "truncation_anchor" for e in warm.events)

    def test_restore_pace_applies_the_parked_cut_for_free(self, transcript, arms):
        # Was the tripwire for the missed free apply (2026-09-25 prod readout):
        # the restore's truncation-anchor save re-stamped updated_at before
        # ``apply_pending_compaction`` read the gap. Now both see the turn's gap.
        restore = simulate(transcript, arms["model_relative"], pace="restore", overhead_tokens=40_000)
        assert any(e["kind"] == "truncation_anchor" for e in restore.events)
        assert restore.policy["lastCut"]["applied"] == "cache_expired"
        assert restore.live_offset > 0

    def test_legacy_on_a_warm_agent_never_slices(self, transcript, arms):
        # Faithful to the pre-1.23.0 bug: legacy sets the checkpoint but only a
        # restore applies it, and a warm agent never restores.
        run = simulate(transcript, arms["legacy"], pace="warm", overhead_tokens=40_000)
        assert run.live_offset == 0
        assert len(run.history) == len(transcript.messages)

    def test_restore_pace_is_deterministic(self, transcript, arms):
        a = simulate(transcript, arms["model_relative"], overhead_tokens=40_000)
        b = simulate(transcript, arms["model_relative"], overhead_tokens=40_000)
        assert a.history == b.history
        assert a.live_offset == b.live_offset

    def test_availability_marks_retained_statements(self, transcript, arms):
        run = simulate(transcript, arms["full"])
        rows = plant_availability(run, transcript)
        assert rows and all(r["statementRetained"] and r["inContext"] for r in rows)

    def test_records_mode_uses_the_provider(self, transcript, arms):
        record = "Standing notes: the password is PLANTED-VALUE-1."
        run = simulate(
            transcript, arms["model_relative"], pace="cold", overhead_tokens=40_000,
            summary_mode="records", records_for_turn=lambda turn: [record],
        )
        assert run.summary == record
        assert run.policy["lastCut"]["summarySource"] == "ltm"


class TestScore:
    @pytest.mark.parametrize("answer,expected,ok", [
        ("$4,750", ("$4,750", "4,750"), True),
        ("4750", ("$4,750", "4,750"), True),
        ("The cap is $4,750.", ("$4,750", "4,750"), True),
        ("17", ("7",), False),
        ("Room 214B", ("Room 214B",), True),
        ("the kestrel foundation", ("the Kestrel Foundation",), True),
    ])
    def test_contains(self, answer, expected, ok):
        assert is_correct(answer, expected) is ok

    def test_forbidden_value_fails_a_superseded_task(self):
        assert is_correct("April 2", ("April 2",), ("March 14",))
        assert not is_correct("It moved from March 14 to April 2", ("April 2",), ("March 14",))
        assert not is_correct("March 14", ("April 2",), ("March 14",))

    def test_mcnemar(self):
        assert mcnemar_exact(0, 0) == 1.0
        assert mcnemar_exact(10, 0) == pytest.approx(2 / 1024)
        assert mcnemar_exact(5, 5) == 1.0

    def test_paired_comparison_counts_losses(self):
        rows = []
        for plant, base_ok, arm_ok in [("p1", True, False), ("p2", True, True), ("p3", False, True), ("p4", True, False)]:
            rows.append({"arm": "full", "variant": 0, "plantId": plant, "family": "constraint", "correct": base_ok})
            rows.append({"arm": "x", "variant": 0, "plantId": plant, "family": "constraint", "correct": arm_ok})
        result = compare(task_outcomes(rows), "x", "full", family="constraint")
        assert (result["n"], result["lossesVsBaseline"], result["winsVsBaseline"]) == (4, 2, 1)

    def test_majority_of_k(self):
        rows = [{"arm": "x", "variant": 0, "plantId": "p", "family": "f", "correct": c} for c in (True, False, True)]
        assert task_outcomes(rows)[("x", 0, "p")]["correct"] is True


class TestAsk:
    def test_request_appends_cache_point_without_mutating_history(self, transcript):
        history = transcript.messages
        before = history[-1]["content"][:]
        request = build_request(history, "Q?", model_id="m", temperature=None)
        assert history[-1]["content"] == before
        assert request["messages"][-2]["content"][-1] == {"cachePoint": {"type": "default"}}
        assert request["messages"][-1] == {"role": "user", "content": [{"text": "Q?"}]}
        assert request["toolConfig"]["tools"]
        assert "temperature" not in request["inferenceConfig"]

    def test_estimate_prices_one_write_then_reads(self):
        est = estimate_cost({(0, "a"): 100_000}, 10, "us.anthropic.claude-haiku-4-5-20251001-v1:0", output_tokens_per_call=0)
        # 100k x 1.25 + 100k x 0.1 x 9 = 215k token-equivalents at $1.10/MTok
        assert est["usd"] == pytest.approx(0.24, abs=0.01)
        assert est["calls"] == 10

    def test_history_tokens_matches_production_estimator(self, transcript):
        from agents.main_agent.session.compaction_policy import estimate_message_tokens

        assert history_tokens(transcript.messages) == sum(estimate_message_tokens(m) for m in transcript.messages)
