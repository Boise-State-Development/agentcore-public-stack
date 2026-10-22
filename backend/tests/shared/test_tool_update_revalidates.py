"""`repository.update_tool` must re-validate the patched state before writing.

The bug this guards: `update_tool` applies a partial update with `setattr`, and
`ToolDefinition` does not set `validate_assignment` — so a `mode="after"` model
validator runs at construction and **never again**. A partial update could
therefore persist a row the model itself considers invalid.

Concretely, `PUT {"alwaysOn": true}` without `enabledByDefault` wrote
`alwaysOn=True` next to `enabledByDefault=False`, the incoherent pair
`_normalize_always_on` exists to prevent. Reads normalised it on the way out,
so nothing misbehaved at runtime — which is exactly why it would have gone
unnoticed. Raw-item consumers (backfills, exports, analytics) do not read
through the model.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from apis.shared.tools.models import ToolDefinition, ToolProtocol
from apis.shared.tools.repository import ToolCatalogRepository


def _tool(**over) -> ToolDefinition:
    base = dict(
        tool_id="t1",
        display_name="T1",
        description="d",
        protocol=ToolProtocol.LOCAL,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    base.update(over)
    return ToolDefinition(**base)


def _repo(existing: ToolDefinition):
    repo = ToolCatalogRepository.__new__(ToolCatalogRepository)
    repo._table = MagicMock()
    written = {}

    def _put(Item):
        written.update(Item)

    repo._table.put_item = MagicMock(side_effect=lambda Item: _put(Item))

    async def _get(tool_id):
        return existing

    repo.get_tool = _get
    return repo, written


class TestSetattrBypassIsRepaired:
    @pytest.mark.asyncio
    async def test_pinning_without_sending_enabled_by_default_stays_coherent(self):
        """The exact shape of the bug: a partial PUT of only `alwaysOn`."""
        repo, written = _repo(_tool(enabled_by_default=False, always_on=False))
        result = await repo.update_tool("t1", {"always_on": True}, "admin")

        assert result.always_on is True
        assert result.enabled_by_default is True, "validator did not re-run"
        assert written["alwaysOn"] is True
        assert written["enabledByDefault"] is True, "incoherent pair reached DynamoDB"

    @pytest.mark.asyncio
    async def test_unpinning_leaves_the_row_coherent(self):
        repo, written = _repo(_tool(enabled_by_default=True, always_on=True))
        result = await repo.update_tool("t1", {"always_on": False}, "admin")

        # Not incoherent — on-by-default without a pin is a legitimate state.
        # Recorded so the asymmetry is deliberate rather than accidental: the
        # validator promotes, it never demotes, and the admin form restores the
        # pre-pin default by sending both flags.
        assert result.always_on is False
        assert result.enabled_by_default is True
        assert written["alwaysOn"] is False

    @pytest.mark.asyncio
    async def test_an_unrelated_update_does_not_disturb_the_flags(self):
        repo, written = _repo(_tool(enabled_by_default=False, always_on=False))
        await repo.update_tool("t1", {"display_name": "Renamed"}, "admin")

        assert written["displayName"] == "Renamed"
        assert written["alwaysOn"] is False
        assert written["enabledByDefault"] is False

    @pytest.mark.asyncio
    async def test_audit_fields_survive_the_revalidation(self):
        """Re-validating round-trips through `model_dump`, so anything the
        dump drops would be silently lost on every update."""
        repo, written = _repo(_tool(created_by="someone"))
        result = await repo.update_tool("t1", {"display_name": "X"}, "admin-42")

        assert result.updated_by == "admin-42"
        assert result.created_by == "someone"
        assert written["createdBy"] == "someone"
        assert written["updatedAt"]
