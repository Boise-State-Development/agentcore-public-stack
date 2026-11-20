"""``WFRUN#`` rows: several handoffs approved once (``docs/specs/workflow-runs.md`` §3.2, §9).

A workflow run lives on its **source** session, like the ``HANDOFF#`` rows of its steps, in
sessions-metadata on the ``SessionLookupIndex`` GSI. No new infra::

    PK:     USER#<user_id>
    SK:     WFRUN#<run_id>
    GSI_PK: SESSION#<source_session_id>     (SessionLookupIndex)
    GSI_SK: WFRUN#<created_at>

The row holds the step list as approved, the files shared, the limits, and empty slots that later
steps fill: ``resultDeliveryId`` (the grouped result, PR-W.2) and ``briefHash`` (one brief per run,
PR-W.3). Each step's own state stays on its ``HANDOFF#`` row, which carries ``runId`` and
``stepId``; a run's status beyond the ones below is derived from those rows when read (spec §3.2).

States, each move a conditional update like ``HandoffStore``'s::

    open ─┬─▶ approved ──▶ stopped
          └─▶ dismissed

An ``open`` run older than ``ABANDON_AFTER`` reads as ``abandoned``, as its unsent step rows do.
Owner scoping is the partition key; a list off the GSI re-checks ``userId``, because a session id
alone does not name a conversation. Rows carry the user's request text, so they expire with the
conversation (365 days, the handoff rows' TTL) and go with the session delete cascade.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, FrozenSet, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

try:  # boto3 is absent in some local-dev setups
    from boto3.dynamodb.conditions import Key
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - exercised only without boto3
    Key = None  # type: ignore[assignment]
    ClientError = Exception  # type: ignore[assignment, misc]

from apis.shared.handoff.models import ABANDON_AFTER, HandoffView

logger = logging.getLogger(__name__)

#: The envelope's limits in step 1 (spec §4). The step count is checked at create; concurrency is
#: the tab's queue's, carried on the row so the SPA and a later server executor read one number.
WORKFLOW_RUN_MIN_STEPS = 2
WORKFLOW_RUN_MAX_STEPS = 8
WORKFLOW_RUN_CONCURRENCY = 4

WorkflowRunStatus = Literal["open", "approved", "dismissed", "stopped"]
EffectiveWorkflowRunStatus = Literal["open", "approved", "dismissed", "stopped", "abandoned"]
#: What a sheet's approval changed, for ``WorkflowRunEdited`` and the row's ``edits``.
WorkflowRunEdit = Literal["request", "step_removed", "file_unticked"]

RUN_TRANSITIONS: Dict[str, FrozenSet[str]] = {
    "open": frozenset({"approved", "dismissed"}),
    "approved": frozenset({"stopped"}),
    "dismissed": frozenset(),
    "stopped": frozenset(),
}

_TTL_DAYS = 365
_SK_PREFIX = "WFRUN#"
_KEY_ATTRS = ("PK", "SK", "GSI_PK", "GSI_SK", "ttl")


class WorkflowRunLimits(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    max_steps: int = Field(WORKFLOW_RUN_MAX_STEPS, alias="maxSteps")
    concurrency: int = WORKFLOW_RUN_CONCURRENCY


class WorkflowRunStep(BaseModel):
    """One step as the run holds it: its Agent, its request, and the handoff row that runs it."""

    model_config = ConfigDict(populate_by_name=True)

    step_id: str = Field(..., alias="stepId")
    handoff_id: str = Field(..., alias="handoffId")
    agent_id: str = Field(..., alias="agentId")
    agent_name: str = Field("", alias="agentName")
    request: str = ""
    removed: bool = False


class WorkflowRun(BaseModel):
    """One ``WFRUN#<runId>`` row, keyed under its owner (``PK=USER#<userId>``)."""

    model_config = ConfigDict(populate_by_name=True)

    run_id: str = Field(..., alias="runId")
    user_id: str = Field(..., alias="userId")
    session_id: str = Field(..., alias="sessionId", description="The source conversation")
    status: WorkflowRunStatus = "open"
    request: str = Field("", description="The shared request; a step's own request may differ")
    steps: List[WorkflowRunStep] = Field(default_factory=list)
    file_ids: List[str] = Field(default_factory=list, alias="fileIds", description="Source files shared with every step")
    limits: WorkflowRunLimits = Field(default_factory=WorkflowRunLimits)
    source_message_index: int = Field(
        -1, alias="sourceMessageIndex", description="Index of the message the run's row sits after; -1 for none"
    )
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")
    approved_at: Optional[str] = Field(None, alias="approvedAt")
    edits: Optional[List[WorkflowRunEdit]] = Field(None, description="What the approval changed at the sheet")
    result_delivery_id: Optional[str] = Field(
        None, alias="resultDeliveryId", description="The grouped result's DELIVERY# row (PR-W.2); empty until then"
    )
    brief_hash: Optional[str] = Field(
        None, alias="briefHash", description="The one brief written for every step (PR-W.3); empty until then"
    )
    brief_tokens: Optional[int] = Field(None, alias="briefTokens", description="The run brief's output tokens")
    brief_cache_status: Optional[str] = Field(
        None, alias="briefCacheStatus", description="The run brief's call's cacheStatus on the source"
    )

    def kept_steps(self) -> List["WorkflowRunStep"]:
        """The steps the run will start: every step not removed at the sheet, in order."""
        return [step for step in self.steps if not step.removed]

    def effective_status(self, now: Optional[datetime] = None) -> EffectiveWorkflowRunStatus:
        """The status as read: an ``open`` run past ``ABANDON_AFTER`` is ``abandoned``, like its steps."""
        if self.status != "open":
            return self.status
        try:
            created = datetime.fromisoformat(self.created_at)
        except ValueError:
            return self.status
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if (now or datetime.now(timezone.utc)) - created >= ABANDON_AFTER:
            return "abandoned"
        return self.status


class WorkflowRunStepView(BaseModel):
    """A step as the SPA reads it: the run's record of it, and its handoff row's current view.

    ``handoff`` is ``None`` only when the step's row is gone (deleted, expired). A step that has
    started has a ``targetSessionId`` there; one that hasn't is ``open``.
    """

    model_config = ConfigDict(populate_by_name=True)

    step_id: str = Field(..., alias="stepId")
    handoff_id: str = Field(..., alias="handoffId")
    agent_id: str = Field(..., alias="agentId")
    agent_name: str = Field(..., alias="agentName")
    request: str
    removed: bool
    handoff: Optional[HandoffView] = None


class WorkflowRunView(BaseModel):
    """A run as the SPA reads it, shared by ``GET …/workflow-runs/{runId}`` and the ``workflowRuns`` replay."""

    model_config = ConfigDict(populate_by_name=True)

    run_id: str = Field(..., alias="runId")
    session_id: str = Field(..., alias="sessionId")
    status: EffectiveWorkflowRunStatus
    request: str
    steps: List[WorkflowRunStepView]
    file_ids: List[str] = Field(..., alias="fileIds")
    limits: WorkflowRunLimits
    source_message_index: int = Field(..., alias="sourceMessageIndex")
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")
    approved_at: Optional[str] = Field(None, alias="approvedAt")
    result_delivery_id: Optional[str] = Field(None, alias="resultDeliveryId")

    @classmethod
    def from_row(cls, run: WorkflowRun, handoffs: Optional[Dict[str, HandoffView]] = None) -> "WorkflowRunView":
        """``handoffs`` maps a handoff id to its row's view; a step whose row is missing has none."""
        views = handoffs or {}
        return cls(
            run_id=run.run_id,
            session_id=run.session_id,
            status=run.effective_status(),
            request=run.request,
            steps=[
                WorkflowRunStepView(
                    step_id=step.step_id,
                    handoff_id=step.handoff_id,
                    agent_id=step.agent_id,
                    agent_name=step.agent_name,
                    request=step.request,
                    removed=step.removed,
                    handoff=views.get(step.handoff_id),
                )
                for step in run.steps
            ],
            file_ids=list(run.file_ids),
            limits=run.limits,
            source_message_index=run.source_message_index,
            created_at=run.created_at,
            updated_at=run.updated_at,
            approved_at=run.approved_at,
            result_delivery_id=run.result_delivery_id,
        )


class WorkflowRunStore:
    """Per-user store of workflow-run rows."""

    def __init__(self, table: Any = None) -> None:
        self._table = table
        if self._table is not None:
            return
        table_name = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
        if not table_name:
            return
        try:
            from apis.shared.aws_clients import get_dynamodb_table

            self._table = get_dynamodb_table(table_name)
        except Exception:  # noqa: BLE001 - dev without AWS creds
            logger.warning("workflow run store: DynamoDB unavailable", exc_info=True)
            self._table = None

    @property
    def enabled(self) -> bool:
        return self._table is not None

    def create(self, run: WorkflowRun) -> WorkflowRun:
        """Write a new row. Raises if the table is missing or the id is already taken."""
        table = self._require_table()
        item = run.model_dump(by_alias=True, exclude_none=True)
        item.update(
            {
                "PK": f"USER#{run.user_id}",
                "SK": f"{_SK_PREFIX}{run.run_id}",
                "GSI_PK": f"SESSION#{run.session_id}",
                "GSI_SK": f"{_SK_PREFIX}{run.created_at}",
                "ttl": int((datetime.now(timezone.utc) + timedelta(days=_TTL_DAYS)).timestamp()),
            }
        )
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(SK)")
        return run

    def get(self, user_id: str, run_id: str) -> Optional[WorkflowRun]:
        """The caller's run, or ``None``. Another user's id reads as absent."""
        table = self._require_table()
        resp = table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"{_SK_PREFIX}{run_id}"})
        item = resp.get("Item")
        return _from_item(item) if item else None

    def list_for_session(self, user_id: str, session_id: str) -> List[WorkflowRun]:
        """This user's runs from one source conversation, oldest first."""
        if self._table is None:
            return []
        items: List[Dict[str, Any]] = []
        kwargs: Dict[str, Any] = {
            "IndexName": "SessionLookupIndex",
            "KeyConditionExpression": Key("GSI_PK").eq(f"SESSION#{session_id}")
            & Key("GSI_SK").begins_with(_SK_PREFIX),
            "ScanIndexForward": True,
        }
        try:
            while True:
                resp = self._table.query(**kwargs)
                items.extend(resp.get("Items", []))
                lek = resp.get("LastEvaluatedKey")
                if not lek:
                    break
                kwargs["ExclusiveStartKey"] = lek
        except ClientError:
            logger.warning("workflow run store: query failed", exc_info=True)
            return []
        return [_from_item(item) for item in items if item.get("userId") == user_id]

    def approve(
        self,
        user_id: str,
        run_id: str,
        *,
        request: str,
        steps: List[WorkflowRunStep],
        file_ids: List[str],
        edits: List[WorkflowRunEdit],
        approved_at: str,
    ) -> Optional[WorkflowRun]:
        """Fix the envelope: ``open`` → ``approved``, exactly once, with the steps as approved.

        ``None`` when the run isn't the caller's or isn't ``open`` (approved twice, or dismissed).
        This is the no-double-approve guard, so it is a condition on the write, not a read before it.
        """
        sets = {
            "request": request,
            "steps": [step.model_dump(by_alias=True) for step in steps],
            "fileIds": list(file_ids),
            "approvedAt": approved_at,
        }
        if edits:
            sets["edits"] = list(edits)
        return self._move(user_id, run_id, "approved", sets, now=approved_at)

    def record_brief(
        self,
        user_id: str,
        run_id: str,
        *,
        brief_hash: str,
        brief_tokens: int,
        brief_cache_status: Optional[str],
    ) -> bool:
        """Record the one brief written for an ``open`` run (spec §6.3). False if it is no longer open.

        The same facts go on each kept step's ``HANDOFF#`` row (``HandoffStore.record_brief``), which
        is what acceptance compares a step's card against; this row's copy is the run's own record.
        A run already approved or dismissed is left alone, as a sent handoff's row is.
        """
        table = self._require_table()
        names = {"#status": "status", "#updatedAt": "updatedAt"}
        values: Dict[str, Any] = {
            ":open": "open",
            ":hash": brief_hash,
            ":tokens": int(brief_tokens),
            ":now": datetime.now(timezone.utc).isoformat(),
        }
        sets = ["briefHash = :hash", "briefTokens = :tokens", "#updatedAt = :now"]
        if brief_cache_status:
            values[":cache"] = brief_cache_status
            sets.append("briefCacheStatus = :cache")
        try:
            table.update_item(
                Key={"PK": f"USER#{user_id}", "SK": f"{_SK_PREFIX}{run_id}"},
                UpdateExpression="SET " + ", ".join(sets),
                ConditionExpression="attribute_exists(SK) AND #status = :open",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                return False
            raise
        return True

    def transition(self, user_id: str, run_id: str, status: WorkflowRunStatus) -> Optional[WorkflowRun]:
        """Move a run to ``status`` (``dismissed`` or ``stopped``) if ``RUN_TRANSITIONS`` allows it.

        ``None`` when the run is missing, someone else's, or in a status the move isn't allowed from.
        Approval goes through ``approve``, which also writes the envelope.
        """
        if status == "approved":
            raise ValueError("approve a run with WorkflowRunStore.approve")
        return self._move(user_id, run_id, status, {})

    def _move(
        self,
        user_id: str,
        run_id: str,
        status: str,
        fields: Dict[str, Any],
        *,
        now: Optional[str] = None,
    ) -> Optional[WorkflowRun]:
        table = self._require_table()
        allowed = sorted(src for src, targets in RUN_TRANSITIONS.items() if status in targets)
        if not allowed:
            return None
        names = {"#status": "status", "#updatedAt": "updatedAt"}
        values: Dict[str, Any] = {":to": status, ":now": now or datetime.now(timezone.utc).isoformat()}
        sets = ["#status = :to", "#updatedAt = :now"]
        for name, value in fields.items():
            names[f"#{name}"] = name
            values[f":{name}"] = value
            sets.append(f"#{name} = :{name}")
        for index, source in enumerate(allowed):
            values[f":from{index}"] = source
        condition = "attribute_exists(SK) AND #status IN ({})".format(
            ", ".join(f":from{index}" for index in range(len(allowed)))
        )
        try:
            resp = table.update_item(
                Key={"PK": f"USER#{user_id}", "SK": f"{_SK_PREFIX}{run_id}"},
                UpdateExpression="SET " + ", ".join(sets),
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                return None
            raise
        return _from_item(resp["Attributes"])

    def delete_for_session(self, user_id: str, session_id: str) -> int:
        """Delete this user's runs from a source conversation. Best-effort; returns the count."""
        if self._table is None:
            return 0
        deleted = 0
        for run in self.list_for_session(user_id, session_id):
            try:
                self._table.delete_item(Key={"PK": f"USER#{user_id}", "SK": f"{_SK_PREFIX}{run.run_id}"})
                deleted += 1
            except ClientError:
                logger.warning("workflow run store: delete failed", exc_info=True)
        return deleted

    def _require_table(self) -> Any:
        if self._table is None:
            from apis.shared.handoff.store import HandoffStoreUnavailable

            raise HandoffStoreUnavailable("DYNAMODB_SESSIONS_METADATA_TABLE_NAME is not configured")
        return self._table


def _from_item(item: Dict[str, Any]) -> WorkflowRun:
    data = {k: v for k, v in item.items() if k not in _KEY_ATTRS}
    if "sourceMessageIndex" in data:
        data["sourceMessageIndex"] = int(data["sourceMessageIndex"])
    if "briefTokens" in data:
        data["briefTokens"] = int(data["briefTokens"])
    limits = data.get("limits")
    if isinstance(limits, dict):
        data["limits"] = {k: int(v) for k, v in limits.items()}
    return WorkflowRun.model_validate(data)


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None) or {}
    return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


_store: Optional[WorkflowRunStore] = None


def get_workflow_run_store() -> WorkflowRunStore:
    """Get or create the process-global workflow run store."""
    global _store
    if _store is None:
        _store = WorkflowRunStore()
    return _store


__all__ = [
    "RUN_TRANSITIONS",
    "WORKFLOW_RUN_CONCURRENCY",
    "WORKFLOW_RUN_MAX_STEPS",
    "WORKFLOW_RUN_MIN_STEPS",
    "WorkflowRun",
    "WorkflowRunEdit",
    "WorkflowRunLimits",
    "WorkflowRunStatus",
    "WorkflowRunStep",
    "WorkflowRunStepView",
    "WorkflowRunStore",
    "WorkflowRunView",
    "get_workflow_run_store",
]
