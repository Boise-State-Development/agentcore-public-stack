"""Retirement metadata on a tool catalog row (mcp-server-retirement §7).

`retirementNote` / `retiresOn` answer "then what?" for a tool being retired.
They are display-only — nothing reads them for access, and neither reaches the
model's `toolConfig` — so the only things worth pinning are that they survive a
DynamoDB round trip, that they are absent-safe on rows written before they
existed, and that a date cannot be stored in a shape the SPA will render as
garbage.
"""

import pytest
from pydantic import ValidationError

from apis.shared.tools.models import ToolDefinition, ToolProtocol, ToolStatus


def _tool(**kw) -> ToolDefinition:
    base = dict(
        tool_id="canvas_faculty",
        display_name="Canvas for Faculty",
        description="Canvas LMS",
        protocol=ToolProtocol.MCP_EXTERNAL,
        status=ToolStatus.DEPRECATED,
    )
    base.update(kw)
    return ToolDefinition(**base)


class TestRoundTrip:
    def test_survives_dynamo_round_trip(self):
        tool = _tool(retirement_note="Replaced by the LMS Middleware", retires_on="2026-10-31")
        back = ToolDefinition.from_dynamo_item(tool.to_dynamo_item())
        assert back.retirement_note == "Replaced by the LMS Middleware"
        assert back.retires_on == "2026-10-31"

    def test_absent_on_an_older_row_reads_as_none(self):
        """Every row written before this shipped lacks both keys."""
        item = _tool().to_dynamo_item()
        del item["retirementNote"]
        del item["retiresOn"]
        back = ToolDefinition.from_dynamo_item(item)
        assert back.retirement_note is None
        assert back.retires_on is None

    def test_blank_normalises_to_none(self):
        """The admin form posts '' for an untouched input, and a tool carrying
        `retirementNote: ''` would render an empty reason line rather than none."""
        tool = _tool(retirement_note="   ", retires_on="")
        assert tool.retirement_note is None
        assert tool.retires_on is None

    def test_a_stored_blank_reads_back_as_none(self):
        """Clearing a note posts '' through `update_tool`, which stores it
        verbatim (it skips None, so '' is the only way to clear). Reading it
        back must normalise, or the SPA sees a value where there is none."""
        item = _tool().to_dynamo_item()
        item["retirementNote"] = ""
        assert ToolDefinition.from_dynamo_item(item).retirement_note is None


class TestRetiresOnValidation:
    @pytest.mark.parametrize("value", ["soon", "10/31/2026", "2026-13-01", "2026-10-32", "31-10-2026"])
    def test_rejects_anything_that_is_not_an_iso_date(self, value):
        """Stored as a string because it is displayed, never computed with — which
        is exactly why it needs a guard. Without one, 'soon' persists happily and
        reaches the SPA, where it renders as an Invalid Date or nothing at all."""
        with pytest.raises(ValidationError):
            _tool(retires_on=value)

    def test_accepts_an_iso_date(self):
        assert _tool(retires_on="2026-10-31").retires_on == "2026-10-31"


class TestIndependenceFromStatus:
    def test_settable_on_an_active_tool_without_changing_anything(self):
        """Not gated on `status`: an admin may fill these in while drafting, and
        `isRetiring` is what decides whether anything is shown. Coupling the two
        would silently discard a note typed before the status was flipped."""
        tool = _tool(status=ToolStatus.ACTIVE, retirement_note="Planned")
        assert tool.status == ToolStatus.ACTIVE
        assert tool.retirement_note == "Planned"
