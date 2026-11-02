"""When ``document_extract`` is mounted, and what it costs to decide.

``docs/specs/structured-extraction.md`` §5.2, §10, D9, D10. The mount decision
sits in ``ChatAgent._create_agent``, which is inside the TTFT budget, so the
cost of *asking* matters as much as the answer. Two properties:

* **Mounted on a field set, not on a document** (D9) — the one deliberate
  divergence from ``verify_sources``' gate. A tool that cannot work costs
  cacheable prefix for nothing.
* **The check does no I/O** (§10). It reads the resource manifest already
  loaded with the skill records, so the preamble does not grow by a network
  round trip per turn.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from agents.main_agent.chat_agent import ChatAgent


class FakeRef:
    def __init__(self, filename: str, kind: str = "reference") -> None:
        self.filename = filename
        self.kind = kind
        self.s3_key = f"skills/x/references/{filename}"


class FakeRecord:
    def __init__(self, skill_id: str, filenames: List[str]) -> None:
        self.skill_id = skill_id
        self.resources = [FakeRef(f) for f in filenames]


class Stub(ChatAgent):
    """A ChatAgent with identity but without ``BaseAgent.__init__``.

    ``_with_document_extract`` reads only the flag, the records and the
    identity, so constructing the whole agent (a session manager, a model, a
    tool registry) would test the harness rather than the gate.
    """

    def __init__(self, session_id: str = "s1", user_id: str = "u1") -> None:
        self.session_id = session_id
        self.user_id = user_id


FIELD_SET_SKILL = [FakeRecord("grants", ["field-set.nsf-solicitation.md"])]
PLAIN_SKILL = [FakeRecord("grants", ["forms.md"])]


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("DOCUMENT_EXTRACT_ENABLED", "true")


class TestTheGate:
    def test_off_by_default_nothing_is_mounted(self, monkeypatch) -> None:
        """In-development semantics: only the literal "true" enables."""
        monkeypatch.delenv("DOCUMENT_EXTRACT_ENABLED", raising=False)
        assert Stub()._with_document_extract([], FIELD_SET_SKILL) == []

    @pytest.mark.parametrize("value", ["", "false", "1", "yes", "on"])
    def test_anything_but_true_leaves_it_off(self, monkeypatch, value) -> None:
        monkeypatch.setenv("DOCUMENT_EXTRACT_ENABLED", value)
        assert Stub()._with_document_extract([], FIELD_SET_SKILL) == []

    def test_on_with_a_field_set_the_tool_is_mounted(self, flag_on) -> None:
        tools = Stub()._with_document_extract([], FIELD_SET_SKILL)
        assert len(tools) == 1
        assert getattr(tools[0], "tool_name", None) == "document_extract"

    def test_on_without_a_field_set_nothing_is_mounted(self, flag_on) -> None:
        """D9. A session with a document and no field set has nothing to
        extract, and the spec is a constant in the cacheable prefix."""
        assert Stub()._with_document_extract([], PLAIN_SKILL) == []

    def test_on_with_no_skills_at_all_nothing_is_mounted(self, flag_on) -> None:
        assert Stub()._with_document_extract([], []) == []

    def test_a_turn_without_identity_mounts_nothing(self, flag_on) -> None:
        """Identity is captured by closure, so there is nothing to bind to."""
        assert Stub(session_id="", user_id="u1")._with_document_extract(
            [], FIELD_SET_SKILL
        ) == []
        assert Stub(session_id="s1", user_id="")._with_document_extract(
            [], FIELD_SET_SKILL
        ) == []

    def test_existing_tools_are_preserved(self, flag_on) -> None:
        tools = Stub()._with_document_extract(["existing"], FIELD_SET_SKILL)
        assert tools[0] == "existing"
        assert len(tools) == 2

    def test_a_failure_leaves_the_tool_list_untouched(self, flag_on, monkeypatch) -> None:
        """Fail-open: a turn without extraction is a working turn, and a raised
        exception during agent construction is not."""
        import apis.shared.extraction.resolver as resolver

        def boom(records: Any) -> bool:
            raise RuntimeError("manifest unreadable")

        monkeypatch.setattr(resolver, "turn_has_field_set", boom)
        assert Stub()._with_document_extract(["kept"], FIELD_SET_SKILL) == ["kept"]


class TestTheGateCostsNothing:
    def test_deciding_to_mount_touches_no_network(self, flag_on, monkeypatch) -> None:
        """§10. The preamble was cut from 455ms to 22-37ms, and each addition
        is several percent of that budget — so the store is replaced with
        something that fails loudly if the gate reaches for it."""
        import apis.shared.extraction.resolver as resolver

        def explode() -> Any:
            raise AssertionError("the mount gate must not read S3")

        monkeypatch.setattr(resolver, "get_skill_resource_store", explode)
        tools = Stub()._with_document_extract([], FIELD_SET_SKILL)
        assert len(tools) == 1

    def test_declining_to_mount_touches_no_network_either(
        self, flag_on, monkeypatch
    ) -> None:
        import apis.shared.extraction.resolver as resolver

        def explode() -> Any:
            raise AssertionError("the mount gate must not read S3")

        monkeypatch.setattr(resolver, "get_skill_resource_store", explode)
        assert Stub()._with_document_extract([], PLAIN_SKILL) == []


class TestTheSkillsRuntimeCarriesRecords:
    def test_the_runtime_is_a_three_element_tuple_and_callers_must_say_so(
        self,
    ) -> None:
        """A three-element tuple does **not** unpack into two names.

        An earlier revision of this change claimed it did and shipped a test
        that only read ``.plugin`` and ``.records`` — so the assertion passed
        while five real call sites raised ``ValueError: too many values to
        unpack``. The named fields are the point of the NamedTuple; the arity
        is a breaking change, and it is written down here rather than assumed
        away.
        """
        from agents.main_agent.skills.strands_mapping import SkillsRuntime

        runtime = SkillsRuntime(None, None, [])
        assert len(runtime) == 3
        plugin, read_tool, records = runtime
        assert (plugin, read_tool, records) == (None, None, [])
        with pytest.raises(ValueError):
            _two, _names = runtime  # noqa: F841 - the arity is the assertion

    def test_an_empty_skill_list_returns_an_empty_runtime(self) -> None:
        from agents.main_agent.skills import strands_mapping as sm

        for empty in (None, []):
            runtime = sm.build_skills_runtime(empty)
            assert runtime.plugin is None
            assert runtime.read_skill_file is None
            assert runtime.records == []

    def test_the_records_are_the_ones_the_plugin_was_built_from(
        self, monkeypatch
    ) -> None:
        """Resolution has to go through exactly the records the disclosure was
        built from, or access stops being structural."""
        from agents.main_agent.skills import strands_mapping as sm

        records = [FakeRecord("grants", ["field-set.awards.md"])]
        monkeypatch.setattr(sm, "fetch_active_skill_records", lambda ids: records)
        monkeypatch.setattr(sm, "record_to_strands_skill", lambda r: object())
        monkeypatch.setattr(sm, "AgentSkills", lambda skills: object())
        monkeypatch.setattr(sm, "make_read_skill_file_tool", lambda recs: recs)

        runtime = sm.build_skills_runtime(["grants"])
        assert runtime.records == records
        assert runtime.read_skill_file == records
