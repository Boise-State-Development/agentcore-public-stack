"""The ``system`` / ``hidden`` catalog fields (Phase 1 of
docs/specs/platform-self-service/design.md).

These add a platform-shipped tool tier on top of the existing ``always_on``
primitive:

* ``system`` — provenance: shipped with the app, not an admin knob. Implies
  ``always_on`` (and therefore ``enabled_by_default``), so every downstream
  reader can trust ``always_on`` alone.
* ``hidden`` — excluded from the user-facing Tools panel toggle list, but kept
  (flagged) in the catalog payload so the SPA can label a tool-use event.

What these tests guard, mirroring the always-on field tests:

1. **Backward compatibility** — a row written before the tier shipped reads
   back as an ordinary, visible, user-toggleable tool.
2. **The validator** — ``system`` makes the incoherent
   ``always_on=False``/``enabled_by_default=False`` pair unrepresentable, on
   read as well as write.
3. **Round trip** — both flags survive DynamoDB.
"""

from apis.shared.tools.models import (
    ToolDefinition,
    ToolProtocol,
    UserToolAccess,
    ToolCategory,
    ToolStatus,
)


def _tool(**overrides) -> ToolDefinition:
    base = dict(
        tool_id="t1",
        display_name="T1",
        description="d",
        protocol=ToolProtocol.LOCAL,
    )
    base.update(overrides)
    return ToolDefinition(**base)


class TestBackwardCompatibleReads:
    def test_row_without_the_attributes_reads_false(self):
        tool = ToolDefinition.from_dynamo_item(
            {"toolId": "legacy", "displayName": "Legacy", "description": "d"}
        )
        assert tool.system is False
        assert tool.hidden is False
        # And an ordinary tool is unaffected by the new normalization branch.
        assert tool.always_on is False
        assert tool.enabled_by_default is False


class TestSystemImpliesAlwaysOn:
    """A system tool is a platform-shipped pin, so it implies always_on."""

    def test_system_forces_always_on_and_default_on_construction(self):
        tool = _tool(system=True)
        assert tool.always_on is True
        assert tool.enabled_by_default is True

    def test_system_forces_always_on_on_read(self):
        # A hand-written item marking only `system` must still normalize.
        tool = ToolDefinition.from_dynamo_item(
            {
                "toolId": "whoami",
                "displayName": "Who am I",
                "description": "d",
                "system": True,
            }
        )
        assert tool.system is True
        assert tool.always_on is True
        assert tool.enabled_by_default is True

    def test_hidden_alone_does_not_imply_always_on(self):
        # `hidden` is a display concern only; it must not pin a tool on.
        tool = _tool(hidden=True)
        assert tool.hidden is True
        assert tool.always_on is False
        assert tool.enabled_by_default is False


class TestRoundTrip:
    def test_flags_survive_a_dynamo_round_trip(self):
        item = _tool(system=True, hidden=True).to_dynamo_item()
        assert item["system"] is True
        assert item["hidden"] is True
        restored = ToolDefinition.from_dynamo_item(item)
        assert restored.system is True
        assert restored.hidden is True
        assert restored.always_on is True  # via the validator


class TestAccountCategory:
    """The account tools (whoami, get_my_quota, get_my_settings) are seeded
    with category='account'. Regression: that value must be a valid
    ToolCategory member, or list_tools() 500s when reading the seeded rows
    back (it built ToolDefinition from every catalog item)."""

    def test_account_is_a_valid_category(self):
        assert ToolCategory.ACCOUNT.value == "account"

    def test_account_category_survives_from_dynamo_item(self):
        tool = ToolDefinition.from_dynamo_item(
            {
                "toolId": "get_my_quota",
                "displayName": "Check my quota",
                "description": "d",
                "category": "account",
                "system": True,
                "hidden": False,
            }
        )
        assert tool.category == ToolCategory.ACCOUNT
        assert tool.system is True


class TestUserToolAccessCarriesHidden:
    def test_hidden_defaults_false_and_round_trips_by_alias(self):
        access = UserToolAccess(
            toolId="whoami",
            displayName="Who am I",
            description="d",
            category=ToolCategory.UTILITY,
            protocol=ToolProtocol.LOCAL,
            status=ToolStatus.ACTIVE,
            grantedBy=["public"],
            enabledByDefault=True,
            hidden=True,
            alwaysOn=True,
            isEnabled=True,
        )
        dumped = access.model_dump(by_alias=True)
        assert dumped["hidden"] is True

    def test_hidden_defaults_false_when_absent(self):
        access = UserToolAccess(
            toolId="search_web",
            displayName="Search",
            description="d",
            category=ToolCategory.SEARCH,
            protocol=ToolProtocol.LOCAL,
            status=ToolStatus.ACTIVE,
            grantedBy=["public"],
            enabledByDefault=True,
            isEnabled=True,
        )
        assert access.hidden is False
