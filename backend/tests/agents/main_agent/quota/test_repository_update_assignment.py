"""Regression tests for QuotaRepository.update_assignment (issue #718).

Editing an assignment's tier used to 400 with
``"QuotaAssignment" object has no field "tierId"`` because the repository
``setattr``-ed the camelCase alias key onto a model whose field is
``tier_id``. These tests lock in that:

* an alias-keyed update (``tierId``) applies without raising, and
* the DynamoDB update expression uses the stored camelCase attribute name
  (``tierId``) with no duplicate snake_case ``tier_id`` — otherwise the item
  would carry both keys and drift from create's ``by_alias=True`` shape.
"""

import pytest
from unittest.mock import MagicMock

from agents.main_agent.quota.repository import QuotaRepository
from agents.main_agent.quota.models import QuotaAssignmentType


def _stored_item(**overrides) -> dict:
    """A DynamoDB item as get_item returns it (camelCase aliases + keys)."""
    item = {
        "PK": "ASSIGNMENT#a1",
        "SK": "METADATA",
        "assignmentId": "a1",
        "tierId": "basic",
        "assignmentType": QuotaAssignmentType.APP_ROLE.value,
        "appRoleId": "faculty",
        "priority": 250,
        "enabled": True,
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
        "createdBy": "admin",
    }
    item.update(overrides)
    return item


def _make_repo() -> QuotaRepository:
    """Repository with a mocked DynamoDB table (no AWS)."""
    repo = QuotaRepository(table_name="quota", events_table_name="quota-events")
    repo.table = MagicMock()
    return repo


@pytest.mark.asyncio
async def test_update_assignment_tier_id_alias_key_does_not_raise():
    """The exact #718 failure: an alias-keyed tier change must apply cleanly."""
    repo = _make_repo()
    repo.table.get_item.return_value = {"Item": _stored_item()}
    repo.table.update_item.return_value = {
        "Attributes": _stored_item(tierId="faculty_tier")
    }

    # Service dumps updates by_alias=True -> {"tierId": ...}
    updated = await repo.update_assignment("a1", {"tierId": "faculty_tier"})

    assert updated is not None
    assert updated.tier_id == "faculty_tier"


@pytest.mark.asyncio
async def test_update_assignment_writes_camelcase_attr_no_snake_duplicate():
    """DynamoDB attribute names stay camelCase; no stray tier_id key."""
    repo = _make_repo()
    repo.table.get_item.return_value = {"Item": _stored_item()}
    repo.table.update_item.return_value = {
        "Attributes": _stored_item(tierId="faculty_tier")
    }

    await repo.update_assignment("a1", {"tierId": "faculty_tier"})

    _, kwargs = repo.table.update_item.call_args
    names = kwargs["ExpressionAttributeNames"]

    # Stored attribute name is the alias, present exactly once.
    assert names.get("#tierId") == "tierId"
    # The broken behavior wrote the snake_case field name instead.
    assert "#tier_id" not in names
    assert "tier_id" not in names.values()


@pytest.mark.asyncio
async def test_update_assignment_field_name_key_also_supported():
    """A field-name key (tier_id) is normalized to the alias for storage too."""
    repo = _make_repo()
    repo.table.get_item.return_value = {"Item": _stored_item()}
    repo.table.update_item.return_value = {
        "Attributes": _stored_item(tierId="faculty_tier")
    }

    updated = await repo.update_assignment("a1", {"tier_id": "faculty_tier"})

    assert updated is not None
    assert updated.tier_id == "faculty_tier"
    _, kwargs = repo.table.update_item.call_args
    names = kwargs["ExpressionAttributeNames"]
    assert names.get("#tierId") == "tierId"
    assert "#tier_id" not in names


@pytest.mark.asyncio
async def test_update_assignment_priority_change_rebuilds_gsi_sort_key():
    """Changing priority must flow into the rebuilt GSI sort key."""
    repo = _make_repo()
    repo.table.get_item.return_value = {"Item": _stored_item()}
    repo.table.update_item.return_value = {
        "Attributes": _stored_item(priority=500)
    }

    await repo.update_assignment("a1", {"priority": 500})

    _, kwargs = repo.table.update_item.call_args
    values = kwargs["ExpressionAttributeValues"]
    # APP_ROLE assignments carry GSI1 (type index) and GSI6 (app-role index).
    assert values[":GSI1SK"] == "PRIORITY#500#a1"
    assert values[":GSI6SK"] == "PRIORITY#500"
