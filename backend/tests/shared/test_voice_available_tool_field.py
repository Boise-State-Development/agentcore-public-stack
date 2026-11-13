"""The ``voiceAvailable`` catalog field (#625).

A tool loads in voice mode only when an admin has marked it as fit for it, so
the safe reading of every row written before the flag is ``False``, and a
partial update that never names the flag must leave it alone.
"""

from apis.shared.tools.models import (
    AdminToolResponse,
    ToolCreateRequest,
    ToolDefinition,
    ToolProtocol,
    ToolUpdateRequest,
)


def _tool(**overrides) -> ToolDefinition:
    base = dict(tool_id="t1", display_name="T1", description="d", protocol=ToolProtocol.LOCAL)
    base.update(overrides)
    return ToolDefinition(**base)


def test_defaults_to_off():
    assert _tool().voice_available is False
    assert ToolCreateRequest(toolId="calc_tool", displayName="C", description="d").voice_available is False


def test_a_row_written_before_the_flag_reads_off():
    tool = ToolDefinition.from_dynamo_item({"toolId": "legacy", "displayName": "Legacy", "description": "d"})
    assert tool.voice_available is False


def test_round_trips_through_dynamo():
    item = _tool(voice_available=True).to_dynamo_item()
    assert item["voiceAvailable"] is True
    assert ToolDefinition.from_dynamo_item(item).voice_available is True


def test_update_request_omitting_the_field_does_not_clear_it():
    dumped = ToolUpdateRequest.model_validate({"displayName": "New"}).model_dump(exclude_unset=True)
    assert "voice_available" not in dumped


def test_update_request_can_set_and_clear_it():
    assert ToolUpdateRequest.model_validate({"voiceAvailable": True}).model_dump(exclude_unset=True)[
        "voice_available"
    ] is True
    assert ToolUpdateRequest.model_validate({"voiceAvailable": False}).model_dump(exclude_unset=True)[
        "voice_available"
    ] is False


def test_admin_response_carries_it():
    body = AdminToolResponse.from_tool_definition(_tool(voice_available=True)).model_dump(by_alias=True)
    assert body["voiceAvailable"] is True
