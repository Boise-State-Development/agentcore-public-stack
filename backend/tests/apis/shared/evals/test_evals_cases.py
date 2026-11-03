"""The golden set loads, and the loader refuses anything that would score nothing.

The committed case files are validated here on every test run, so a malformed
case fails CI rather than a release-day run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apis.shared.evals.cases import (
    FAMILIES,
    CaseError,
    load_cases,
    parse_case,
    required_grants,
)

BACKEND = Path(__file__).resolve().parents[4]
CASES_DIR = BACKEND / "evals" / "cases"
FIXTURES_DIR = BACKEND / "evals" / "fixtures"


def _case(**overrides):
    raw = {
        "id": "tool_selection.example",
        "family": "tool_selection",
        "turns": [{"prompt": "What is 2 + 2?"}],
        "run": {"enabled_tools": ["calculator"]},
        "expect": {"contains_any": ["4"], "tools": {"must_call": ["calculator"]}},
        "notes": "",
    }
    raw.update(overrides)
    return raw


class TestCommittedGoldenSet:
    def test_every_committed_case_loads_with_its_fixtures(self):
        cases = load_cases(CASES_DIR, fixtures_dir=FIXTURES_DIR)
        assert len(cases) >= 40
        assert len({c.id for c in cases}) == len(cases)

    def test_every_gating_family_has_enough_cases_to_ever_fail(self):
        # The gate's small-family clause needs a few cases to mean anything;
        # spec §9 targets eight to twelve per gating family.
        cases = load_cases(CASES_DIR)
        for family in FAMILIES:
            if family == "general_chat":
                continue
            assert sum(1 for c in cases if c.family == family) >= 8, family

    def test_file_names_are_families(self):
        for path in CASES_DIR.glob("*.json"):
            assert path.stem in FAMILIES, path.name

    def test_required_grants_name_the_tools_the_cases_offer(self):
        grants = required_grants(load_cases(CASES_DIR))
        assert "calculator" in grants["tools"]
        assert set(grants["skills"]) == {"web_research", "skill_creator"}


class TestParseCase:
    def test_a_minimal_case_parses(self):
        case = parse_case(_case(), family="tool_selection")
        assert case.turns == ("What is 2 + 2?",)
        assert case.run.enabled_tools == ("calculator",)
        assert case.run.enabled_skills is None
        assert case.gating

    def test_unknown_keys_are_errors_not_ignored(self):
        # A misspelt guard would otherwise become a case that can never fail.
        raw = _case(expect={"contains_any": ["4"], "tools": {"must_not_cal": ["whoami"]}})
        with pytest.raises(CaseError, match="unknown key"):
            parse_case(raw, family="tool_selection")

    def test_id_must_carry_its_family(self):
        with pytest.raises(CaseError, match="family"):
            parse_case(_case(id="retention.example"), family="tool_selection")

    def test_a_gating_case_must_check_something(self):
        with pytest.raises(CaseError, match="deterministic check"):
            parse_case(_case(expect={}), family="tool_selection")

    def test_a_judged_case_must_name_a_judge(self):
        raw = _case(id="general_chat.example", family="general_chat", expect={})
        with pytest.raises(CaseError, match="evaluator or a rubric"):
            parse_case(raw, family="general_chat")
        raw["judge"] = {"evaluators": ["helpfulness"]}
        assert not parse_case(raw, family="general_chat").gating

    def test_unknown_evaluator_is_rejected(self):
        raw = _case(judge={"evaluators": ["vibes"]})
        with pytest.raises(CaseError, match="unknown"):
            parse_case(raw, family="tool_selection")

    def test_required_tool_must_be_offered(self):
        raw = _case(run={"enabled_tools": ["whoami"]})
        with pytest.raises(CaseError, match="not in run.enabled_tools"):
            parse_case(raw, family="tool_selection")

    def test_required_skill_must_be_offered(self):
        raw = _case(
            id="skill_selection.example",
            family="skill_selection",
            run={"enabled_skills": ["skill_creator"]},
            expect={"skill": {"loads": ["web_research"]}},
        )
        with pytest.raises(CaseError, match="not in run.enabled_skills"):
            parse_case(raw, family="skill_selection")

    def test_a_tool_cannot_be_both_required_and_forbidden(self):
        raw = _case(expect={"tools": {"must_call": ["calculator"], "must_not_call": ["calculator"]}})
        with pytest.raises(CaseError, match="both required and forbidden"):
            parse_case(raw, family="tool_selection")

    def test_invalid_regex_is_caught_at_load(self):
        raw = _case(expect={"regex": {"search": ["(unclosed"]}})
        with pytest.raises(CaseError, match="invalid regex"):
            parse_case(raw, family="tool_selection")

    def test_empty_tool_list_means_none_not_defaults(self):
        case = parse_case(_case(run={"enabled_tools": []}, expect={"contains_any": ["4"]}), family="tool_selection")
        assert case.run.enabled_tools == ()


class TestLoadCases:
    def _write(self, tmp_path: Path, family: str, cases):
        (tmp_path / f"{family}.json").write_text(json.dumps(cases), encoding="utf-8")

    def test_duplicate_ids_across_the_set_are_rejected(self, tmp_path):
        self._write(tmp_path, "tool_selection", [_case(), _case()])
        with pytest.raises(CaseError, match="duplicate"):
            load_cases(tmp_path)

    def test_unknown_family_file_is_rejected(self, tmp_path):
        self._write(tmp_path, "vibes", [])
        with pytest.raises(CaseError, match="unknown family"):
            load_cases(tmp_path)

    def test_missing_fixture_is_rejected(self, tmp_path):
        self._write(tmp_path, "tool_selection", [_case(files=["nope.md"])])
        with pytest.raises(CaseError, match="nope.md"):
            load_cases(tmp_path, fixtures_dir=tmp_path)

    def test_family_filter(self, tmp_path):
        self._write(tmp_path, "tool_selection", [_case()])
        retention = _case(id="retention.example", family="retention", run={"enabled_tools": []},
                          expect={"contains_any": ["x"]})
        self._write(tmp_path, "retention", [retention])
        assert [c.family for c in load_cases(tmp_path, families=["retention"])] == ["retention"]
        with pytest.raises(CaseError, match="unknown"):
            load_cases(tmp_path, families=["nope"])
