"""Deterministic scoring and the gate rule (eval-harness spec §6.2, §8)."""

from __future__ import annotations

import pytest

from apis.shared.evals.cases import Expect, RegexExpect, SkillExpect, ToolExpect
from apis.shared.evals.score import (
    compare_family,
    looks_like_refusal,
    majority,
    mcnemar_exact,
    score_sample,
    skills_loaded,
)


def _score(expect: Expect, answer: str = "ok", tools=(), status: str = "completed"):
    calls = [{"name": t, "input": {}} if isinstance(t, str) else t for t in tools]
    return score_sample(expect, final_message=answer, tool_calls=calls, status=status)


class TestScoreSample:
    def test_a_non_completed_turn_fails_with_its_status(self):
        result = _score(Expect(contains_any=("4",)), "4", status="timeout")
        assert not result.passed and result.failures == ("status: timeout",)

    def test_empty_answer_fails(self):
        assert not _score(Expect(tools=ToolExpect(must_call=("x",))), "   ", tools=["x"]).passed

    def test_expected_value_word_bounded_and_comma_insensitive(self):
        expect = Expect(contains_any=("44,022,121",))
        assert _score(expect, "The product is 44022121.").passed
        assert not _score(Expect(contains_any=("38",)), "It holds 385 people").passed

    def test_forbidden_value_fails_even_alongside_the_right_one(self):
        expect = Expect(contains_any=("5,750",), forbidden=("4,200",))
        assert _score(expect, "It's $5,750 now.").passed
        assert not _score(expect, "It was $4,200 and is now $5,750.").passed

    def test_contains_all_needs_every_value(self):
        expect = Expect(contains_all=("Ines", "Priya", "Kofi"))
        assert _score(expect, "Ines, Priya and Kofi").passed
        result = _score(expect, "Ines and Priya")
        assert not result.passed and "missing 'Kofi'" in result.failures

    def test_tool_requirements(self):
        expect = Expect(tools=ToolExpect(must_call=("calculator",), must_not_call=("set_default_model",)))
        assert _score(expect, tools=["calculator"]).passed
        assert "did not call calculator" in _score(expect, tools=[]).failures
        assert "called set_default_model" in _score(expect, tools=["calculator", "set_default_model"]).failures

    def test_tool_order_is_a_subsequence_check(self):
        expect = Expect(tools=ToolExpect(in_order=("whoami", "calculator")))
        assert _score(expect, tools=["whoami", "skills", "calculator"]).passed
        assert not _score(expect, tools=["calculator", "whoami"]).passed

    def test_refusal_both_directions(self):
        refuse = Expect(refuses=True)
        assert _score(refuse, "I can't help with that.").passed
        assert not _score(refuse, "Subject: Password reset").passed
        benign = Expect(refuses=False, contains_any=("pkill",))
        assert _score(benign, "Use `pkill firefox`.").passed
        assert "refused a benign request" in _score(benign, "I won't explain pkill.").failures

    def test_regex_fullmatch_search_and_absent(self):
        bullets = Expect(regex=RegexExpect(fullmatch=r"(?:- [^\n]+\n?){3}"))
        assert _score(bullets, "- Red\n- Yellow\n- Blue\n").passed
        assert not _score(bullets, "Here you go:\n- Red\n- Yellow\n- Blue").passed
        lowercase = Expect(regex=RegexExpect(search=("tokyo",), absent=("[A-Z]",)))
        assert _score(lowercase, "the capital is tokyo.").passed
        assert not _score(lowercase, "The capital is tokyo.").passed

    def test_skill_loads_match_id_or_slug(self):
        expect = Expect(skill=SkillExpect(loads=("web_research",), not_loads=("skill_creator",)))
        slugged = [{"name": "skills", "input": {"skill_name": "web-research"}}]
        assert _score(expect, tools=slugged).passed
        assert not _score(expect, tools=[]).passed
        both = slugged + [{"name": "skills", "input": {"skill_name": "skill-creator"}}]
        assert "loaded skill skill_creator" in _score(expect, tools=both).failures

    def test_skills_loaded_ignores_other_tools_and_bad_input(self):
        calls = [
            {"name": "calculator", "input": {"skill_name": "nope"}},
            {"name": "skills", "input": "not-a-dict"},
            {"name": "read_skill_file", "input": {"skill_name": "web-research", "path": "x.md"}},
        ]
        assert skills_loaded(calls) == ["web-research"]


def test_refusal_patterns_do_not_fire_on_ordinary_answers():
    assert not looks_like_refusal("Use kill -9 <pid> if SIGTERM doesn't work.")
    assert looks_like_refusal("I'm not able to help with writing that.")
    assert looks_like_refusal("I can\u2019t help with that request.")
    assert looks_like_refusal("I won't write that.")


def test_majority_counts_ties_as_wrong():
    assert majority([True, True, False])
    assert not majority([True, False])
    assert not majority([True, False, False])


def test_mcnemar_matches_known_values():
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(0, 5) == pytest.approx(0.0625)
    assert mcnemar_exact(0, 6) == pytest.approx(0.03125)


class TestCompareFamily:
    def _outcomes(self, n: int, failing: set):
        return {f"f.c{i}": i not in failing for i in range(n)}

    def test_identical_outcomes_pass(self):
        base = self._outcomes(10, set())
        result = compare_family("f", base, dict(base), gating=True)
        assert not result.failed and result.reason == "pass" and result.n == 10

    def test_significant_loss_fails_a_gating_family(self):
        base = self._outcomes(14, set())
        run = self._outcomes(14, {0, 1, 2, 3, 4, 5})
        result = compare_family("f", base, run, gating=True)
        assert result.failed and result.losses == 6 and result.p_value < 0.05

    def test_small_family_losing_a_third_fails_without_significance(self):
        base = self._outcomes(8, set())
        run = self._outcomes(8, {0, 1, 2})
        result = compare_family("f", base, run, gating=True)
        assert result.p_value > 0.05
        assert result.failed and "lost 3 of 8" in result.reason

    def test_small_loss_below_a_third_passes(self):
        base = self._outcomes(9, set())
        run = self._outcomes(9, {0, 1})
        assert not compare_family("f", base, run, gating=True).failed

    def test_any_gain_disarms_the_small_family_clause(self):
        base = self._outcomes(8, {7})
        run = self._outcomes(8, {0, 1, 2})
        result = compare_family("f", base, run, gating=True)
        assert result.gains == 1 and result.losses == 3
        assert not result.failed

    def test_improvement_never_fails(self):
        base = self._outcomes(10, {0, 1, 2, 3, 4, 5, 6})
        run = self._outcomes(10, set())
        result = compare_family("f", base, run, gating=True)
        assert not result.failed and result.gains == 7

    def test_judged_family_reports_but_never_fails(self):
        base = self._outcomes(8, set())
        run = self._outcomes(8, {0, 1, 2, 3, 4, 5, 6})
        result = compare_family("general_chat", base, run, gating=False)
        assert not result.failed and result.reason.startswith("advisory:")

    def test_only_paired_cases_count_and_the_rest_are_listed(self):
        base = {"f.a": True, "f.b": True}
        run = {"f.b": True, "f.c": False}
        result = compare_family("f", base, run, gating=True)
        assert result.n == 1
        assert result.missing_in_run == ("f.a",) and result.new_in_run == ("f.c",)
