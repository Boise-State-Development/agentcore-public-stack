"""The ``addedBy`` backfill decides each project skill's adder from the settings history
and never guesses (shared-projects §9.6, PR #1780 follow-up)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from backfill_project_skill_adders import (  # noqa: E402
    apply_adders,
    resolve_adders,
    unattributed_skill_refs,
)

from apis.shared.assistants.models import AgentBinding, AgentVersion, Assistant  # noqa: E402


def _harness(*bindings: AgentBinding) -> Assistant:
    return Assistant(
        assistantId="ast-harness", ownerId="u-creator", ownerName="Creator", name="P", description="d",
        instructions="i", vectorIndexId="idx", visibility="PRIVATE", createdAt="t", updatedAt="t",
        status="COMPLETE", kind="project", bindings=list(bindings),
    )


def _version(number: int, created_by, *refs: str) -> AgentVersion:
    return AgentVersion(
        agentId="ast-harness", version=number, createdBy=created_by, name="P", description="d", instructions="i",
        bindings=[AgentBinding(kind="skill", ref=r, config={"version": 1}) for r in refs],
    )


def test_the_first_version_binding_a_skill_names_its_adder():
    harness = _harness(
        AgentBinding(kind="skill", ref="owner_style", config={"version": 1}),
        AgentBinding(kind="skill", ref="erins_rubric", config={"version": 1}),
        AgentBinding(kind="tool", ref="web_search"),
    )
    # Newest first, as the repository lists them: the owner added hers in version 2, Erin
    # added hers in version 3, and the owner re-saved the list in version 4. The initial
    # snapshot (version 1) bound nothing.
    versions = [
        _version(4, "u-owner", "owner_style", "erins_rubric"),
        _version(3, "u-erin", "owner_style", "erins_rubric"),
        _version(2, "u-owner", "owner_style"),
        _version(1, None),
    ]

    result = resolve_adders(harness, versions)

    assert result.adders == {"owner_style": "u-owner", "erins_rubric": "u-erin"}
    assert result.unknown == []


def test_a_skill_in_the_initial_snapshot_is_left_unknown_not_attributed_to_the_creator():
    harness = _harness(AgentBinding(kind="skill", ref="old_skill", config={"version": 1}))
    result = resolve_adders(harness, [_version(1, None, "old_skill"), _version(2, "u-owner", "old_skill")])
    assert result.adders == {}
    assert result.unknown == ["old_skill"]


def test_a_skill_in_no_version_is_unknown_and_an_attributed_one_is_untouched():
    harness = _harness(
        AgentBinding(kind="skill", ref="never_saved", config={}),
        AgentBinding(kind="skill", ref="done", config={"version": 2, "addedBy": "u-erin"}),
    )
    result = resolve_adders(harness, [_version(2, "u-owner", "done")])
    assert result.adders == {}
    assert result.unknown == ["never_saved"]
    assert result.attributed == ["done"]
    assert unattributed_skill_refs(harness.bindings) == ["never_saved"]


def test_a_pinned_ref_is_judged_on_its_base_id():
    harness = _harness(AgentBinding(kind="skill", ref="pdf_workflows@3", config={}))
    result = resolve_adders(harness, [_version(1, None), _version(2, "u-erin", "pdf_workflows")])
    assert result.adders == {"pdf_workflows": "u-erin"}


def test_apply_writes_only_the_resolved_adders_and_keeps_everything_else():
    bindings = [
        AgentBinding(kind="skill", ref="owner_style", config={"version": 1}),
        AgentBinding(kind="skill", ref="done", config={"version": 2, "addedBy": "u-erin"}),
        AgentBinding(kind="tool", ref="web_search", config={}),
    ]
    out = apply_adders(bindings, {"owner_style": "u-owner", "done": "u-someone-else"})
    assert [b.model_dump(by_alias=True) for b in out] == [
        {"kind": "skill", "ref": "owner_style", "config": {"version": 1, "addedBy": "u-owner"}},
        {"kind": "skill", "ref": "done", "config": {"version": 2, "addedBy": "u-erin"}},
        {"kind": "tool", "ref": "web_search", "config": {}},
    ]
