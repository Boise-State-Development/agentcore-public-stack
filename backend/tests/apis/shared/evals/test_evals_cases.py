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
    PROVISIONED_TOOLS,
    CaseError,
    grant_for_tool,
    load_cases,
    parse_case,
    required_grants,
)

BACKEND = Path(__file__).resolve().parents[4]
CASES_DIR = BACKEND / "evals" / "cases"


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
    def test_every_committed_case_loads(self):
        # That every fixture a case names exists, committed or generated, is
        # pinned in tests/test_eval_fixtures.py, which can import the builder.
        cases = load_cases(CASES_DIR)
        assert len(cases) >= 40
        assert len({c.id for c in cases}) == len(cases)

    def test_every_gating_family_has_enough_cases_to_ever_fail(self):
        # The gate's small-family clause needs a few cases to mean anything;
        # spec §9 targets eight to twelve per gating family.
        from apis.shared.evals.cases import ADVISORY_FAMILIES

        cases = load_cases(CASES_DIR)
        for family in FAMILIES:
            if family in ADVISORY_FAMILIES:
                continue
            assert sum(1 for c in cases if c.family == family) >= 8, family

    def test_file_names_are_families(self):
        for path in CASES_DIR.glob("*.json"):
            assert path.stem in FAMILIES, path.name

    def test_required_grants_name_the_tools_the_cases_offer(self):
        grants = required_grants(load_cases(CASES_DIR))
        assert "calculator" in grants["tools"]
        assert set(grants["skills"]) == {"web_research", "skill_creator"}

    def test_required_grants_hold_catalog_ids_never_provisioned_tool_names(self):
        tools = set(required_grants(load_cases(CASES_DIR))["tools"])
        assert not tools & set(PROVISIONED_TOOLS), "a provisioned tool name is not a catalog id the role can grant"
        assert "create_powerpoint_presentation" in tools


class TestProvisionedTools:
    """Tools a case requires that a gate or another catalog id provisions."""

    def test_a_deck_tool_is_reachable_through_the_id_that_provisions_it(self):
        raw = _case(run={"enabled_tools": ["create_powerpoint_presentation"]},
                    expect={"contains_any": ["14"], "tools": {"must_call": ["read_powerpoint_presentation"]}})
        assert parse_case(raw, family="tool_selection").expect.tools.must_call == ("read_powerpoint_presentation",)

    def test_without_that_id_it_is_unreachable(self):
        raw = _case(run={"enabled_tools": []},
                    expect={"contains_any": ["14"], "tools": {"must_call": ["read_powerpoint_presentation"]}})
        with pytest.raises(CaseError, match="not in run.enabled_tools"):
            parse_case(raw, family="tool_selection")

    def test_document_read_needs_no_grant(self):
        raw = _case(run={"enabled_tools": []},
                    expect={"contains_any": ["x"], "tools": {"must_call": ["document_read"]}})
        case = parse_case(raw, family="tool_selection")
        assert grant_for_tool("document_read") is None
        assert required_grants([case])["tools"] == []

    def test_a_catalog_tool_grants_itself(self):
        assert grant_for_tool("analyze_spreadsheet") == "analyze_spreadsheet"


class TestGeneratedFixtures:
    def test_a_generated_name_satisfies_the_fixture_check(self, tmp_path):
        (tmp_path / "tool_selection.json").write_text(json.dumps([{**_case(), "files": ["built.png"]}]))
        with pytest.raises(CaseError, match="not found"):
            load_cases(tmp_path, fixtures_dir=tmp_path)
        assert len(load_cases(tmp_path, fixtures_dir=tmp_path, generated=["built.png"])) == 1


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


class TestFixtureRuns:
    def test_a_case_may_run_against_a_project_or_an_agent(self):
        case = parse_case(_case(run={"project": "kb_small"}), family="tool_selection")
        assert case.run.project == "kb_small" and case.run.agent is None

    def test_not_both(self):
        with pytest.raises(CaseError, match="not both"):
            parse_case(_case(run={"project": "kb_small", "agent": "agent_format"}), family="tool_selection")

    def test_a_fixture_name_is_a_short_name(self):
        with pytest.raises(CaseError, match="fixture name"):
            parse_case(_case(run={"project": "[eval] kb_small"}), family="tool_selection")

    def test_fixture_keys_name_each_fixture_once(self):
        from apis.shared.evals.cases import fixture_key, fixture_keys

        a = parse_case(_case(id="tool_selection.a", run={"project": "kb_small"}), family="tool_selection")
        b = parse_case(_case(id="tool_selection.b", run={"agent": "agent_format"}), family="tool_selection")
        c = parse_case(_case(id="tool_selection.c"), family="tool_selection")
        assert fixture_keys([a, b, a, c]) == ["agent:agent_format", "project:kb_small"]
        assert fixture_key(c) is None


class TestMaxCalls:
    def test_parses(self):
        case = parse_case(_case(expect={"contains_any": ["4"], "tools": {"max_calls": 2}}), family="tool_selection")
        assert case.expect.tools.max_calls == 2

    def test_cannot_be_lower_than_the_tools_it_must_call(self):
        raw = _case(expect={"contains_any": ["4"], "tools": {"must_call": ["calculator"], "max_calls": 0}})
        with pytest.raises(CaseError, match="max_calls"):
            parse_case(raw, family="tool_selection")

    def test_rejects_a_non_integer(self):
        with pytest.raises(CaseError, match="max_calls"):
            parse_case(_case(expect={"contains_any": ["4"], "tools": {"max_calls": "3"}}), family="tool_selection")


def _memory_case(**overrides):
    raw = {
        "id": "project_memory.example",
        "family": "project_memory",
        "turns": [{"prompt": "Remember X."}],
        "run": {"project": "@ephemeral"},
        "expect": {"memory": [{"scope": "mine", "contains_any": ["X"]}]},
    }
    raw.update(overrides)
    return raw


class TestProjectMemoryFormat:
    def test_a_memory_case_parses(self):
        case = parse_case(_memory_case(), family="project_memory")
        assert case.run.project == "@ephemeral" and case.expect.memory[0].scope == "mine"
        assert case.meta_for(0).session == 1 and case.meta_for(0).as_user == "eval"

    def test_memory_checks_need_a_project_of_their_own(self):
        with pytest.raises(CaseError, match="project of its own"):
            parse_case(_memory_case(run={"project": "kb_small"}), family="project_memory")

    def test_an_empty_memory_check_is_refused(self):
        with pytest.raises(CaseError, match="must check something"):
            parse_case(_memory_case(expect={"memory": [{"scope": "mine"}]}), family="project_memory")

    def test_an_unknown_scope_is_refused(self):
        with pytest.raises(CaseError, match="scope"):
            parse_case(_memory_case(expect={"memory": [{"scope": "team", "contains_any": ["X"]}]}), family="project_memory")

    def test_a_member2_turn_needs_a_role(self):
        turns = [{"prompt": "Hi", "as": "member2"}]
        with pytest.raises(CaseError, match="member2_role"):
            parse_case(_memory_case(turns=turns), family="project_memory")

    def test_a_session_belongs_to_one_user(self):
        turns = [{"prompt": "A"}, {"prompt": "B", "as": "member2"}]
        with pytest.raises(CaseError, match="already belongs"):
            parse_case(_memory_case(turns=turns, run={"project": "@ephemeral", "member2_role": "editor"}),
                       family="project_memory")

    def test_seeded_memory_has_a_body(self):
        run = {"project": "@ephemeral", "project_memory": {"project": {"a": {"description": "x"}}}}
        with pytest.raises(CaseError, match="body"):
            parse_case(_memory_case(run=run), family="project_memory")

    def test_the_memory_tools_need_no_grant(self):
        case = parse_case(_memory_case(expect={"tools": {"must_call": ["memory_save"]}}), family="project_memory")
        assert required_grants([case])["tools"] == []

    def test_an_ephemeral_project_is_not_a_seeded_fixture(self):
        from apis.shared.evals.cases import fixture_key, fixture_keys

        case = parse_case(_memory_case(), family="project_memory")
        assert fixture_keys([case]) == [] and fixture_key(case) is None


class TestTerminalFormat:
    def _raw(self, **run):
        return {"id": "terminal.x", "family": "terminal", "turns": [{"prompt": "Read a file"}],
                "run": {"client_surface": "terminal", **run}, "expect": {"tools": {"must_call": ["local_read_file"]}}}

    def test_a_terminal_case_is_advisory_and_its_client_tools_need_no_grant(self):
        case = parse_case(self._raw(client_tools=[{"name": "read_file", "description": "Read.", "respond": {"output": "x"}}],
                                    enabled_tools=[]), family="terminal")
        assert not case.gating and required_grants([case])["tools"] == []

    def test_a_client_tool_needs_an_answer(self):
        with pytest.raises(CaseError, match="respond"):
            parse_case(self._raw(client_tools=[{"name": "read_file", "description": "Read.", "respond": {}}]),
                       family="terminal")

    def test_an_unknown_surface_is_refused(self):
        with pytest.raises(CaseError, match="client_surface"):
            parse_case(self._raw(client_surface="tui"), family="terminal")


# --------------------------------------------------------------------------- delivery arms (§8.4)


def _delivery(**run):
    return {
        "id": "attachment_delivery.example",
        "family": "attachment_delivery",
        "turns": [{"prompt": "What is the permit?"}],
        "run": {"enabled_tools": [], **run},
        "expect": {"contains_any": ["WP-1"]},
        "notes": "",
    }


def test_a_delivery_case_defaults_to_the_auto_arm_and_the_deployments_ceiling():
    case = parse_case(_delivery(), family="attachment_delivery")
    assert (case.run.arm, case.run.ceiling) == ("auto", "default")
    assert not case.gating


@pytest.mark.parametrize("key,value", [
    ("arm", "native"), ("arm", "text"), ("arm", "on_demand"),
    ("ceiling", "200k"), ("ceiling", "100k_documents_excluded"),
])
def test_the_forced_arms_and_the_ceiling_arms_parse(key, value):
    case = parse_case(_delivery(**{key: value}), family="attachment_delivery")
    assert getattr(case.run, key) == value


def test_search_stays_reserved_until_phase_five():
    with pytest.raises(CaseError, match="reserved: the legacy profile"):
        parse_case(_delivery(arm="search"), family="attachment_delivery")


def test_every_ceiling_names_its_runtime_environment():
    from apis.shared.evals.cases import CEILING_ENV, CEILINGS

    assert set(CEILING_ENV) == set(CEILINGS) and CEILING_ENV["default"] == {}
    assert CEILING_ENV["100k_documents_excluded"]["AGENTCORE_MEMORY_COMPACTION_CEILING_EXCLUDES_DOCUMENTS"] == "true"


def test_a_pause_is_a_delivery_case_s_and_bounded():
    raw = _delivery()
    raw["turns"] = [{"prompt": "one"}, {"prompt": "two", "pause_seconds": 330}]
    case = parse_case(raw, family="attachment_delivery")
    assert [case.meta_for(i).pause_seconds for i in range(2)] == [0, 330]
    raw["turns"][1]["pause_seconds"] = 5_000
    with pytest.raises(CaseError, match="pause_seconds"):
        parse_case(raw, family="attachment_delivery")
    other = _case()
    other["turns"] = [{"prompt": "one", "pause_seconds": 10}]
    with pytest.raises(CaseError, match="only attachment_delivery cases pause"):
        parse_case(other, family="tool_selection")


def test_an_unknown_arm_and_an_arm_outside_the_family_are_refused():
    with pytest.raises(CaseError, match="expected one of auto, native"):
        parse_case(_delivery(arm="fast"), family="attachment_delivery")
    with pytest.raises(CaseError, match="only attachment_delivery cases name a arm"):
        parse_case(_case(run={"enabled_tools": ["calculator"], "arm": "auto"}), family="tool_selection")


def test_the_delivery_family_is_opt_in():
    from apis.shared.evals.cases import DEFAULT_FAMILIES, OPT_IN_FAMILIES

    assert "attachment_delivery" in OPT_IN_FAMILIES and "attachment_delivery" not in DEFAULT_FAMILIES
    assert set(DEFAULT_FAMILIES) | OPT_IN_FAMILIES == set(FAMILIES)
