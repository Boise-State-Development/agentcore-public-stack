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
``PK=USER#{user_id}, SK=RUN#{run_id}``. The session listing queries
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
    ) -> None:
        self._table().put_item(
            Item={
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
        )

    def record_end(self, *, result: RunResult) -> None:
        self._table().update_item(
            Key={"PK": f"USER#{result.user_id}", "SK": f"RUN#{result.run_id}"},
            UpdateExpression=(
                "SET #s = :s, stopReason = :sr, errorDetail = :e, "
                "toolNames = :t, #u = :u, finishedAt = :f, "
                "finalMessageChars = :c"
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
            },
        )


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
    ) -> None:
        """AUDIT — implemented. Failure here fails the run (a run that can't
        be audited must not execute unattended)."""
        self._audit.record_start(
            run_id=run_id,
            user_id=user_id,
            session_id=session_id,
            trigger=trigger,
            prompt=prompt,
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
