"""`injected_tools_are_key_described` — which turns may reuse a cached Agent.

The agent cache key carries session, user and a hash of enabled_tools. A
per-request tool builder that closes over only those produces tools equivalent
to freshly-built ones, so the cached agent is safe to reuse. One that captures
anything else (an assistant id, a memory binding) is not.

Getting this predicate wrong in the permissive direction is a correctness bug —
an agent answering against the wrong assistant's corpus or the wrong memory
space — so these lean on the "unless proven, bypass" default.
"""

from apis.shared.tools.injected import (
    ARTIFACT_TOOL_IDS,
    INJECTED_TOOL_IDS,
    KEY_DESCRIBED_INJECTED_TOOL_IDS,
    SPREADSHEET_TOOL_IDS,
    injected_tools_are_key_described,
)


class TestKeyDescribedPredicate:
    def test_an_artifact_only_turn_is_key_described(self):
        assert injected_tools_are_key_described(
            ["create_artifact"], has_memory_binding=False
        )

    def test_a_turn_with_no_injected_tools_is_trivially_key_described(self):
        assert injected_tools_are_key_described(["web_search"], has_memory_binding=False)
        assert injected_tools_are_key_described(None, has_memory_binding=False)
        assert injected_tools_are_key_described([], has_memory_binding=False)

    def test_spreadsheet_tools_are_not_key_described(self):
        # They close over `assistant_id`, which the key does not carry.
        for tool_id in SPREADSHEET_TOOL_IDS:
            assert not injected_tools_are_key_described(
                [tool_id], has_memory_binding=False
            )

    def test_one_unkeyed_builder_disqualifies_the_whole_turn(self):
        # The agent is one object; a single unkeyed capture taints it.
        assert not injected_tools_are_key_described(
            ["create_artifact", "analyze_spreadsheet"], has_memory_binding=False
        )

    def test_a_memory_binding_vetoes_regardless_of_enabled_tools(self):
        # Memory-Space tools are gated on the binding, not on enabled_tools, so
        # no id in the list can represent them — the caller must say so.
        assert not injected_tools_are_key_described(
            ["create_artifact"], has_memory_binding=True
        )
        assert not injected_tools_are_key_described([], has_memory_binding=True)

    def test_every_family_still_awaiting_promotion_bypasses(self):
        """The experiment measures one variable (spec §6).

        Word/Excel/PowerPoint/workspace capture only session+user, so they are
        eligible on the same reasoning as artifacts — but they stay out until the
        artifact arm reads clean. This fails the day someone widens the set
        without revisiting the experiment.
        """
        for tool_id in INJECTED_TOOL_IDS - KEY_DESCRIBED_INJECTED_TOOL_IDS:
            assert not injected_tools_are_key_described(
                [tool_id], has_memory_binding=False
            ), f"{tool_id} was promoted without updating the experiment"

    def test_the_experiment_arm_is_artifacts_only(self):
        assert KEY_DESCRIBED_INJECTED_TOOL_IDS == ARTIFACT_TOOL_IDS

    def test_accepts_a_set_as_well_as_a_list(self):
        # Callers pass whatever `effective_enabled_tools` happens to be.
        assert injected_tools_are_key_described(
            {"create_artifact"}, has_memory_binding=False
        )
        assert not injected_tools_are_key_described(
            frozenset({"analyze_spreadsheet"}), has_memory_binding=False
        )
