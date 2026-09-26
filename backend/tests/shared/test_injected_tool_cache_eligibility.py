"""`injected_tools_are_key_described` — which turns may reuse a cached Agent.

The agent cache key carries session, user, assistant and a hash of
enabled_tools. A per-request tool builder that closes over only those produces
tools equivalent to freshly-built ones, so the cached agent is safe to reuse.
One that captures anything else is not, unless the key carries it: Memory-Space
tools close over the resolved binding, which `_create_cache_key` carries as the
`memory_binding` element (Shared Projects 2.1), so it is no longer a veto here.

Getting this predicate wrong in the permissive direction is a correctness bug —
an agent answering against the wrong assistant's corpus or the wrong memory
space — so these lean on the "unless proven, bypass" default.
"""

from apis.shared.tools.injected import (
    ARTIFACT_TOOL_IDS,
    EXCEL_SPREADSHEET_TOOL_IDS,
    INJECTED_TOOL_IDS,
    KEY_DESCRIBED_INJECTED_TOOL_IDS,
    POWERPOINT_PRESENTATION_TOOL_IDS,
    SPREADSHEET_TOOL_IDS,
    WORD_DOCUMENT_TOOL_IDS,
    WORKSPACE_TOOL_IDS,
    injected_tools_are_key_described,
)


class TestKeyDescribedPredicate:
    def test_an_artifact_only_turn_is_key_described(self):
        assert injected_tools_are_key_described(
            ["create_artifact"]
        )

    def test_a_turn_with_no_injected_tools_is_trivially_key_described(self):
        assert injected_tools_are_key_described(["web_search"])
        assert injected_tools_are_key_described(None)
        assert injected_tools_are_key_described([])

    def test_spreadsheet_tools_are_key_described(self):
        # They close over `assistant_id`, which `_create_cache_key` now carries
        # (and `PausedTurnSnapshot` replays on resume).
        for tool_id in SPREADSHEET_TOOL_IDS:
            assert injected_tools_are_key_described(
                [tool_id]
            )

    def test_one_unkeyed_builder_disqualifies_the_whole_turn(self, monkeypatch):
        # The agent is one object; a single unkeyed capture taints it. Every
        # current family is described, so model a future one that is injected
        # but not (yet) keyed.
        import apis.shared.tools.injected as injected

        monkeypatch.setattr(
            injected, "INJECTED_TOOL_IDS", injected.INJECTED_TOOL_IDS | {"future_unkeyed_tool"}
        )
        assert not injected.injected_tools_are_key_described(
            ["create_artifact", "future_unkeyed_tool"]
        )
        assert injected.injected_tools_are_key_described(["create_artifact"])

    def test_session_only_families_are_key_described(self):
        """Word/Excel/PowerPoint/workspace close over `(session_id, user_id)`
        only — both key elements — so they are eligible on exactly the artifact
        reasoning. Promoted once the artifact arm read clean (spec §8)."""
        for tool_id in (
            WORD_DOCUMENT_TOOL_IDS
            | EXCEL_SPREADSHEET_TOOL_IDS
            | POWERPOINT_PRESENTATION_TOOL_IDS
            | WORKSPACE_TOOL_IDS
        ):
            assert injected_tools_are_key_described(
                [tool_id]
            ), f"{tool_id} closes over session+user only and must be cacheable"

    def test_a_turn_mixing_every_promoted_family_is_key_described(self):
        assert injected_tools_are_key_described(
            [
                "create_artifact",
                "create_word_document",
                "create_excel_spreadsheet",
                "create_powerpoint_presentation",
                "workspace_files",
                "web_search",
            ],
        )

    def test_every_enabled_tools_gated_family_is_key_described(self):
        """Fails the day a new injected family is added without deciding its
        cache eligibility: a new builder that closes over something the key
        does not carry must be left OUT of the described set until it does."""
        assert INJECTED_TOOL_IDS - KEY_DESCRIBED_INJECTED_TOOL_IDS == frozenset()

    def test_artifacts_remain_in_the_set(self):
        assert ARTIFACT_TOOL_IDS <= KEY_DESCRIBED_INJECTED_TOOL_IDS

    def test_accepts_a_set_as_well_as_a_list(self):
        # Callers pass whatever `effective_enabled_tools` happens to be.
        assert injected_tools_are_key_described(
            {"create_artifact"}
        )
        assert injected_tools_are_key_described(
            frozenset({"analyze_spreadsheet"})
        )
