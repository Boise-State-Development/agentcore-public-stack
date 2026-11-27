"""A schedule's action audit, as its owner reads it (docs/specs/proactive-trust.md §7.4).

The Runtime writes one ``RUNACT#{runId}#{toolUseId}`` row per call an unattended run makes
that is not known to be read-only (``RunAuditRecorder.record_action``). This module reads a
schedule's rows back for ``GET /schedules/{id}/actions``: one Query on the existing
``SessionLookupIndex``, ``GSI_PK = RUNACTS#{userId}#{scheduleId}``, newest first. The user
is in the partition, so a listing can never read another user's rows.

Ids, classes, outcomes and times only: a row carries no input and no result.

Blocking, like ``runs``: callers use ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from apis.shared.harness.governance import action_partition

from . import service as _service

logger = logging.getLogger(__name__)

#: The most rows ``GET /schedules/{id}/actions`` returns.
DEFAULT_ACTIONS_LIMIT = 50


class ScheduleAction(BaseModel):
    """One audited call of a schedule's run."""

    model_config = ConfigDict(populate_by_name=True)

    run_id: str = Field(..., alias="runId")
    session_id: Optional[str] = Field(None, alias="sessionId")
    tool_use_id: str = Field(..., alias="toolUseId")
    trigger: Optional[str] = None
    tool_name: str = Field(..., alias="toolName")
    tool_id: Optional[str] = Field(None, alias="toolId")
    server_name: Optional[str] = Field(None, alias="serverName")
    #: ``write`` or ``unclassified`` (not marked read-only).
    call_class: str = Field(..., alias="class")
    why: Optional[str] = None
    #: ``not_required`` or ``approved``; ``approvedVia`` is ``inbox`` or ``conversation``.
    approval: str = "not_required"
    approved_via: Optional[str] = Field(None, alias="approvedVia")
    #: ``started`` (may have run), ``ok`` or ``error``.
    outcome: str = "started"
    started_at: str = Field(..., alias="startedAt")
    finished_at: Optional[str] = Field(None, alias="finishedAt")
    rounds: int = 1

    @field_validator("rounds", mode="before")
    @classmethod
    def _int(cls, value: object) -> int:
        return int(value) if value is not None else 1


def list_schedule_actions(user_id: str, schedule_id: str, limit: int = DEFAULT_ACTIONS_LIMIT) -> List[ScheduleAction]:
    """The schedule's audited calls, newest first, at most ``limit``."""
    from boto3.dynamodb.conditions import Key

    response = _service._get_table().query(
        IndexName="SessionLookupIndex",
        KeyConditionExpression=Key("GSI_PK").eq(action_partition(user_id, schedule_id)),
        ScanIndexForward=False,
        Limit=limit,
    )
    actions: List[ScheduleAction] = []
    for item in response.get("Items", []):
        try:
            actions.append(ScheduleAction.model_validate(item))
        except Exception as e:
            logger.warning(f"Failed to parse schedule action item: {e}")
    return actions
