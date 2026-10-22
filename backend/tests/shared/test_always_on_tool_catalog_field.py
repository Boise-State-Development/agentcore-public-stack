"""The ``alwaysOn`` catalog field (PR-1 of docs/specs/admin-always-on-tools.md).

PR-1 ships the field as **inert data**: nothing reads it yet. What these tests
guard is the property that makes that safe to deploy (§10):

> Deploying to an environment where no tool is marked ``alwaysOn`` must be a
> no-op — byte-identical ``toolConfig``, identical agent cache keys, no new
> DynamoDB writes, no change to the tool picker.

and the encoding that pays for choosing two booleans over a three-state enum
(§2.2): the normalizing validator, which is load-bearing until the
``toolEnablement`` migration lands (§10.3) and must not be deleted as
redundant.
"""

import pytest

from apis.inference_api.chat.routes import _with_auto_enabled_tools
from apis.shared.tools.models import (
    MCPToolEntry,
    MCPToolEntryPayload,
    ToolDefinition,
    ToolProtocol,
    ToolUpdateRequest,
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
    """A row written before always-on shipped must behave exactly as it did."""

    def test_catalog_row_without_the_attribute_reads_false(self):
        tool = ToolDefinition.from_dynamo_item(
            {"toolId": "legacy", "displayName": "Legacy", "description": "d"}
        )
        assert tool.always_on is False
        assert tool.enabled_by_default is False

    def test_mcp_entry_dict_without_the_attribute_reads_false(self):
        assert MCPToolEntry.from_dict({"name": "a"}).always_on is False

    def test_legacy_bare_string_entry_reads_false(self):
        # `_parse_mcp_tools` still accepts the oldest stored shape (List[str]).
        assert MCPToolEntry(name="a").always_on is False

    def test_update_request_omitting_the_field_does_not_clear_it(self):
        """The compat property §10.1 calls load-bearing.

        The admin update route dumps with ``exclude_unset=True``, so a field an
        older client never sends must be absent from the dump — not present as
        ``False``, which would silently clear an admin's flag.
        """
        req = ToolUpdateRequest.model_validate({"displayName": "New name"})
        dumped = req.model_dump(exclude_unset=True, by_alias=False)
        assert "always_on" not in dumped

    def test_update_request_sending_the_field_carries_it(self):
        req = ToolUpdateRequest.model_validate({"alwaysOn": True})
        assert req.model_dump(exclude_unset=True, by_alias=False)["always_on"] is True

    def test_update_request_can_explicitly_clear_it(self):
        """False must survive the dump — distinguishable from "not sent"."""
        req = ToolUpdateRequest.model_validate({"alwaysOn": False})
        assert req.model_dump(exclude_unset=True, by_alias=False)["always_on"] is False


class TestNormalizingValidator:
    """§2.2 — the invalid pair must be unrepresentable, on read AND on write.

    ⚠️ If these fail because the validator was removed as redundant, read §10.3
    before deleting them: the validator retires with the enum migration, not
    before.
    """

    def test_always_on_forces_enabled_by_default_on_construction(self):
        tool = _tool(always_on=True, enabled_by_default=False)
        assert tool.enabled_by_default is True

    def test_always_on_forces_enabled_by_default_on_read(self):
        # A hand-written DynamoDB item must not be able to produce the pair.
        tool = ToolDefinition.from_dynamo_item(
            {
                "toolId": "z",
                "displayName": "Z",
                "description": "d",
                "alwaysOn": True,
                "enabledByDefault": False,
            }
        )
        assert tool.always_on is True
        assert tool.enabled_by_default is True

    def test_a_tool_that_is_not_always_on_is_left_alone(self):
        tool = _tool(always_on=False, enabled_by_default=False)
        assert tool.enabled_by_default is False

    def test_default_on_without_always_on_stays_unpinned(self):
        tool = _tool(always_on=False, enabled_by_default=True)
        assert tool.always_on is False
        assert tool.enabled_by_default is True


class TestRoundTrip:
    def test_flag_survives_a_dynamo_round_trip(self):
        item = _tool(always_on=True).to_dynamo_item()
        assert item["alwaysOn"] is True
        assert ToolDefinition.from_dynamo_item(item).always_on is True

    def test_mcp_entry_flag_survives_the_wire_payload(self):
        payload = MCPToolEntryPayload.from_model(MCPToolEntry(name="c", always_on=True))
        assert payload.model_dump(by_alias=True)["alwaysOn"] is True
        assert payload.to_model().always_on is True


class TestDeployIsANoOp:
    """§10's acceptance criterion, asserted by **identity** rather than equality.

    With nothing to add, ``_with_auto_enabled_tools`` returns the very object it
    was handed. That is what makes the no-op structural: ``None`` stays ``None``,
    so the agent cache key, the freshness hash and the serialized ``toolConfig``
    are not merely equal to today's but the same values computed from the same
    object. An implementation that rebuilt an equal list would pass an equality
    assertion and still be a regression waiting for a downstream identity check.
    """

    def test_none_stays_none(self):
        assert _with_auto_enabled_tools(None, []) is None

    def test_empty_list_is_returned_unchanged(self):
        original: list = []
        assert _with_auto_enabled_tools(original, []) is original

    def test_populated_list_is_returned_unchanged(self):
        original = ["search_web", "browse_web"]
        assert _with_auto_enabled_tools(original, []) is original

    def test_already_present_ids_do_not_rebuild_the_list(self):
        original = ["search_web", "browse_web"]
        assert _with_auto_enabled_tools(original, ["search_web"]) is original

    @pytest.mark.parametrize("enabled", [None, [], ["search_web"]])
    def test_no_always_on_tools_configured_means_no_change(self, enabled):
        """The shape PR-2 will call: an empty resolved set changes nothing."""
        assert _with_auto_enabled_tools(enabled, []) is enabled
