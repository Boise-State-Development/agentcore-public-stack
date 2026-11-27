"""Governance floor (F6a) seams for headless runs.

The moment an agent turn runs unattended *as a user*, it touches user data
with no human in the loop — so every headless run passes through this floor.
The "Run now" phase (PR-1) ships **audit-only + wired seams** (locked
decision, agentic-platform-primitives.md §6): the audit checkpoints are
implemented and fail-closed on start; the guardrails and
data-classification checkpoints are deliberate no-op seams whose
implementations are gated to the *scheduled* (fully unattended) trigger in
a later phase. "Run now" is user-initiated and attended — the human is in
the loop — so the audit trail is the floor it needs.

Hook points (called by ``run_agent_headless`` in this order):

1. ``on_run_start``  — AUDIT (implemented): who/what/when/trigger, before
   any token is minted or model called. **Fail-closed**: no audit record →
   no run.
2. ``check_input``   — GUARDRAILS seam (no-op): the scheduled-runs phase
   calls Bedrock ``ApplyGuardrail`` on the prompt here; a blocked verdict
   raises before the run spends tokens or reads user data.
3. ``classify_output`` — DATA-CLASSIFICATION seam (no-op): the
   scheduled-runs phase runs the PII/FERPA checkpoint over the final
   message (and tool-result previews) here, before delivery.
4. ``on_run_end``    — AUDIT (implemented): outcome, stop reason, tool
   names, usage. Best-effort.

Audit records are written to the sessions-metadata table under
``PK=USER#{user_id}, SK=RUN#{run_id}``. The Runtime also writes one action row per
unattended call that is not known to be read-only, ``SK=RUNACT#{run_id}#{toolUseId}``
(``record_action``, docs/specs/proactive-trust.md §7). The session listing queries
``begins_with(SK, 'S#ACTIVE#')`` and the GSI lookups use their own key
shapes, so ``RUN#`` items are invisible to every existing access path.
When the scheduler lands (Phase B), promote run-records to their own table
(or a documented SK family) with a "recent runs per user" GSI.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import boto3

from apis.shared.harness.models import RunResult

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class RunAuditRecorder:
    """Minimal durable audit trail for headless runs (the implemented slice)."""

    def __init__(
        self, *, table_name: Optional[str] = None, region: Optional[str] = None
    ) -> None:
        self._table_name = table_name or os.environ.get(
            "DYNAMODB_SESSIONS_METADATA_TABLE_NAME", ""
        )
        self._region = region or os.environ.get("AWS_REGION", "us-west-2")

    def _table(self):
        if not self._table_name:
            raise RuntimeError(
                "DYNAMODB_SESSIONS_METADATA_TABLE_NAME is required for run auditing"
            )
        return boto3.resource("dynamodb", region_name=self._region).Table(
            self._table_name
        )

    def record_start(
        self,
        *,
        run_id: str,
        user_id: str,
        session_id: str,
        trigger: str,
        prompt: str,
        title: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        item: dict[str, Any] = {
            "PK": f"USER#{user_id}",
            "SK": f"RUN#{run_id}",
            "runId": run_id,
            "userId": user_id,
            "sessionId": session_id,
            "trigger": trigger,
            "promptSha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "promptChars": len(prompt),
            "status": "started",
            "startedAt": _now_iso(),
        }
        if title:
            item["title"] = title
        # Ids and kinds only (an inbox answer's ``resumesRunId`` and ``decisions``), never
        # content. They never replace a key the row is built from.
        for key, value in (extra or {}).items():
            item.setdefault(key, value)
        self._table().put_item(Item=item)

    def end_if_started(self, *, user_id: str, run_id: str, status: str, error: str) -> bool:
        """End a run row that nothing finished, if it still reads ``started``.

        For a resume whose worker died (answer-from-inbox §3.5): a retried event that
        finds the start mark ends the row here, so a tracker polling ``GET /runs/{id}``
        stops at once. Never overwrites a row the run itself ended.
        """
        from botocore.exceptions import ClientError

        try:
            self._table().update_item(
                Key={"PK": f"USER#{user_id}", "SK": f"RUN#{run_id}"},
                UpdateExpression="SET #s = :s, errorDetail = :e, finishedAt = :f",
                ConditionExpression="#s = :started",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": status, ":e": error[:1000], ":f": _now_iso(), ":started": "started"},
            )
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def record_end(self, *, result: RunResult) -> None:
        self._table().update_item(
            Key={"PK": f"USER#{result.user_id}", "SK": f"RUN#{result.run_id}"},
            UpdateExpression=(
                "SET #s = :s, stopReason = :sr, errorDetail = :e, "
                "toolNames = :t, #u = :u, finishedAt = :f, "
                "finalMessageChars = :c, title = :ti"
            ),
            ExpressionAttributeNames={"#s": "status", "#u": "usage"},
            ExpressionAttributeValues={
                ":s": result.status,
                ":sr": result.stop_reason or "",
                ":e": (result.error or "")[:1000],
                ":t": [t.name for t in result.tool_trace],
                # Dynamo rejects floats; usage payloads are ints but guard
                # against provider metrics like latency ratios.
                ":u": _dynamo_safe(result.usage),
                ":f": _now_iso(),
                ":c": len(result.final_message),
                ":ti": result.title or "",
            },
        )

    # ------------------------------------------------------------------ actions
    #
    # The action audit (docs/specs/proactive-trust.md §7.1): one ``RUNACT#`` row per
    # call an unattended run makes that is not known to be read-only, written by the
    # Runtime's ``HeadlessActionAudit`` middleware *before* the call runs. Ids,
    # classes, a hash and a length; never the input or the result.

    def _action_table(self):
        """The table through the process's cached DynamoDB resource.

        ``record_action`` sits before a call that is about to run (and, on one
        attended resume, before the first token), so it never builds a client.
        """
        from apis.shared.aws_clients import get_dynamodb_table

        if not self._table_name:
            raise RuntimeError(
                "DYNAMODB_SESSIONS_METADATA_TABLE_NAME is required for the action audit"
            )
        return get_dynamodb_table(self._table_name, self._region)

    def record_action(
        self,
        *,
        user_id: str,
        run_id: str,
        session_id: str,
        tool_use_id: str,
        trigger: str,
        tool_name: str,
        call_class: str,
        why: str,
        approval: str,
        tool_input: Any = None,
        schedule_id: Optional[str] = None,
        tool_id: Optional[str] = None,
        server_name: Optional[str] = None,
        approved_via: Optional[str] = None,
        approving_run_id: Optional[str] = None,
    ) -> str:
        """Write the call's row before it runs; ``"recorded"``, or ``"replayed"`` for a row that exists.

        Keyed on the run and the tool use, and conditional on the row not existing,
        so an MCP form's replay of the same call, or a resume of it, adds a round to
        the one row instead of a second row. Raises on any other failure: the
        caller must not run the call it could not record.
        """
        from botocore.exceptions import ClientError

        now = _now_iso()
        text = canonical_input(tool_input)
        key = action_key(user_id, run_id, tool_use_id)
        item: dict[str, Any] = {
            **key,
            "GSI_PK": action_partition(user_id, schedule_id),
            "GSI_SK": now,
            "runId": run_id,
            "sessionId": session_id,
            "toolUseId": tool_use_id,
            "trigger": trigger,
            "toolName": tool_name,
            "class": call_class,
            "why": why,
            "approval": approval,
            "inputSha256": hashlib.sha256(text.encode()).hexdigest(),
            "inputChars": len(text),
            "outcome": "started",
            "startedAt": now,
            "rounds": 1,
            "ttl": int(datetime.now(timezone.utc).timestamp()) + ACTION_TTL_SECONDS,
        }
        for name, value in (
            ("scheduleId", schedule_id),
            ("toolId", tool_id),
            ("serverName", server_name),
            ("approvedVia", approved_via),
            ("approvingRunId", approving_run_id),
        ):
            if value:
                item[name] = value
        table = self._action_table()
        try:
            table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
            return "recorded"
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
        # The same call again (a form's replay, a resume): one more round on its row.
        # An approval this pass carries is kept; an earlier one is never undone.
        sets = ["outcome = :started"]
        values: dict[str, Any] = {":one": 1, ":started": "started"}
        if approval == "approved":
            sets.append("approval = :ap")
            values[":ap"] = approval
            if approved_via:
                sets.append("approvedVia = :via")
                values[":via"] = approved_via
            if approving_run_id:
                sets.append("approvingRunId = :arid")
                values[":arid"] = approving_run_id
        table.update_item(
            Key=key,
            UpdateExpression="SET " + ", ".join(sets) + " ADD #rounds :one",
            ExpressionAttributeNames={"#rounds": "rounds"},
            ExpressionAttributeValues=values,
        )
        return "replayed"

    def finish_action(self, *, user_id: str, run_id: str, tool_use_id: str, outcome: str) -> None:
        """The call's outcome, after it ran. The caller treats a failure as best-effort.

        A lost update leaves ``outcome: started``, which reads as "may have run",
        never as "didn't". Writes only over a row that exists.
        """
        self._action_table().update_item(
            Key=action_key(user_id, run_id, tool_use_id),
            UpdateExpression="SET outcome = :o, finishedAt = :f",
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues={":o": outcome, ":f": _now_iso()},
        )

    def get(self, *, user_id: str, run_id: str) -> Optional[dict[str, Any]]:
        """The caller's own run record, or ``None``.

        Keyed on ``USER#{user_id}``, so another user's ``run_id`` reads as
        missing rather than needing a separate ownership check.
        """
        response = self._table().get_item(
            Key={"PK": f"USER#{user_id}", "SK": f"RUN#{run_id}"}
        )
        return response.get("Item")


#: How long an action row is kept (proactive-trust §12 Q8): ``apis/shared/audit``'s retention.
ACTION_TTL_SECONDS = 365 * 24 * 60 * 60
#: The listing partition of a run that no schedule started (Run now).
RUN_NOW_PARTITION = "run_now"


def action_key(user_id: str, run_id: str, tool_use_id: str) -> dict[str, str]:
    return {"PK": f"USER#{user_id}", "SK": f"RUNACT#{run_id}#{tool_use_id}"}


def action_partition(user_id: str, schedule_id: Optional[str]) -> str:
    """The ``SessionLookupIndex`` partition a schedule's actions are listed from.

    The user is in it, so one user's listing can never read another's rows through a
    missing filter, and Run now's rows don't share one partition across all users.
    """
    return f"RUNACTS#{user_id}#{schedule_id or RUN_NOW_PARTITION}"


def canonical_input(value: Any) -> str:
    """A call's input as stable JSON text: what ``inputSha256`` and ``inputChars`` describe."""
    import json

    if value is None:
        return ""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _dynamo_safe(value: Any) -> Any:
    if isinstance(value, float):
        return str(value)
    if isinstance(value, dict):
        return {k: _dynamo_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dynamo_safe(v) for v in value]
    return value


class GovernanceFloor:
    """F6a checkpoint bundle every headless run passes through."""

    def __init__(self, *, audit: Optional[RunAuditRecorder] = None) -> None:
        self._audit = audit or RunAuditRecorder()

    async def on_run_start(
        self,
        *,
        run_id: str,
        user_id: str,
        session_id: str,
        trigger: str,
        prompt: str,
        title: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        """AUDIT — implemented. Failure here fails the run (a run that can't
        be audited must not execute unattended). ``extra`` adds ids and
        kinds to the row, never content."""
        kwargs: dict[str, Any] = {"extra": extra} if extra else {}
        self._audit.record_start(
            run_id=run_id,
            user_id=user_id,
            session_id=session_id,
            trigger=trigger,
            prompt=prompt,
            title=title,
            **kwargs,
        )

    async def check_input(self, *, prompt: str, user_id: str) -> None:
        """GUARDRAILS seam — deliberate no-op for attended "Run now".

        Scheduled-runs phase: Bedrock ``ApplyGuardrail`` (source=INPUT) on
        the prompt; raise a ``GovernanceBlocked`` error on a blocked
        verdict so the runner records an audited, non-retryable failure
        before any token is spent. The runner already calls this on every
        run, so filling it in requires no control-flow change.
        """

    async def classify_output(self, *, result: RunResult) -> None:
        """DATA-CLASSIFICATION seam — deliberate no-op for attended "Run now".

        Scheduled-runs phase: PII/FERPA checkpoint over
        ``result.final_message`` and ``result.tool_trace`` previews before
        delivery. May redact (mutate the result) or block (raise), per
        policy. The runner already calls this before delivery on every run.
        """

    async def on_run_end(self, *, result: RunResult) -> None:
        """AUDIT — implemented. Best-effort: a failed end-write must not
        destroy an otherwise-delivered result (the start record already
        pins the run's existence)."""
        try:
            self._audit.record_end(result=result)
        except Exception:
            logger.error(
                "Failed to write run-end audit record for %s",
                result.run_id,
                exc_info=True,
            )
