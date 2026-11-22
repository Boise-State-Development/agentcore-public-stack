"""A workflow run's prompt and checklist steps: the request that runs them and the writes on their rows.

``docs/specs/workflow-designer.md`` §21.3, §22.3. A prompt step is a ``HANDOFF#`` row of kind ``prompt``:
one model call over the run's files, with no Agent and no conversation. A checklist step is a row of
kind ``checklist``: one call per field set it names, with the same three moves below. The Runtime runs the steps
a ``workflow_prompt`` invocation names (``inference_api/chat/workflow_prompt.py``). This module
holds the request's shape and the three moves the invocation makes on a step's row::

    open ──claim──▶ running ──finish──▶ done
                       └─────stop─────▶ stopped (stoppedReason)

Each move is one conditional ``UpdateItem`` under the caller's own ``USER#`` partition, so the
condition is the guard: two invocations for one step (two tabs, a resend) both try the claim and
exactly one wins, and a step that finished never moves again. No session lease is taken; the
claim is what keeps a step from running twice.

The row is written by attribute name, with the names ``models.py`` defines for a prompt row
(``PROMPT_STEP_FIELDS``), and every move follows ``PROMPT_TRANSITIONS``. ``updatedAt`` on the ``done`` write is the
finish time a run's collect reads.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

try:  # boto3 is absent in some local-dev setups
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - exercised only without boto3
    ClientError = Exception  # type: ignore[assignment, misc]

from apis.shared.handoff.context import context_hash
from apis.shared.handoff.delivery import SEND_BACK_MAX_CHARS, cap_delivery_text
from apis.shared.handoff.models import (
    KIND_PROMPT_STEP,
    MODEL_STEP_KINDS,
    PROMPT_FIELD_OUTPUT_HASH,
    PROMPT_FIELD_OUTPUT_TEXT,
    PROMPT_FIELD_OUTPUT_TOKENS,
    PROMPT_FIELD_STOPPED_REASON,
    PROMPT_FIELD_UPDATED_AT,
    PROMPT_STOP_REASONS,
    PromptStopReason,
)

logger = logging.getLogger(__name__)

#: Step ids one ``workflow_prompt`` request may name: a run's whole envelope.
WORKFLOW_PROMPT_MAX_STEPS = 8

_SK_PREFIX = "HANDOFF#"

#: The last line of an answer the model was still writing when it reached the output limit, so a
#: reader of the step, and a step or merge that reads it, can tell it is incomplete.
CUT_OFF_MARKER = "[cut off at the output limit]"


def mark_cut_off(text: str) -> str:
    """``text`` capped as a stored answer is, with ``CUT_OFF_MARKER`` as its last line.

    Room is kept for the marker inside ``SEND_BACK_MAX_CHARS``, so the cap ``finish`` applies (and
    any later delivery cap) never cuts the marker off.
    """
    capped = cap_delivery_text(text)
    room = SEND_BACK_MAX_CHARS - len(CUT_OFF_MARKER) - 2
    if len(capped) > room:
        capped = capped[:room].rstrip()
    return f"{capped}\n\n{CUT_OFF_MARKER}"


class WorkflowPromptRequest(BaseModel):
    """``workflow_prompt`` on ``/invocations``: run these prompt steps of one approved run.

    Sent on the run's **source** session. Optional and additive on ``InvocationRequest``; a
    request without it is unchanged.
    """

    model_config = ConfigDict(populate_by_name=True)

    run_id: str = Field(..., alias="runId", min_length=1, max_length=64)
    step_ids: List[str] = Field(..., alias="stepIds", min_length=1, max_length=WORKFLOW_PROMPT_MAX_STEPS)

    @field_validator("step_ids")
    @classmethod
    def _distinct_ids(cls, value: List[str]) -> List[str]:
        seen: Dict[str, None] = {}
        for step_id in value:
            if not isinstance(step_id, str) or not 0 < len(step_id) <= 64:
                raise ValueError("each step id is 1 to 64 characters")
            seen.setdefault(step_id, None)
        return list(seen)


class PromptStepStore:
    """The claim, finish and stop writes on a prompt step's ``HANDOFF#`` row."""

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
            logger.warning("prompt steps: DynamoDB unavailable", exc_info=True)
            self._table = None

    @property
    def enabled(self) -> bool:
        return self._table is not None

    def claim(self, user_id: str, handoff_id: str, *, run_id: str, kind: str = KIND_PROMPT_STEP) -> bool:
        """``open`` → ``running``, exactly once. False if the step was claimed, settled, or isn't this run's step of ``kind``.

        ``kind`` is ``prompt`` or ``checklist`` (``MODEL_STEP_KINDS``); the condition holds the row to
        it, so a checklist step is never claimed as a prompt step or the other way round.
        """
        if kind not in MODEL_STEP_KINDS:
            raise ValueError(f"not a model step kind: {kind}")
        return self._update(
            user_id,
            handoff_id,
            sets={"#status": ("status", "running")},
            condition="#status = :from0 AND #kind = :kind AND #runId = :runId",
            condition_values={":from0": "open", ":kind": kind, ":runId": run_id},
            condition_names={"#kind": "kind", "#runId": "runId"},
        )

    def finish(self, user_id: str, handoff_id: str, *, output_text: str, output_tokens: int) -> bool:
        """``running`` → ``done`` with the answer: capped as a delivery part is, hashed, and its tokens."""
        text = cap_delivery_text(output_text)
        return self._update(
            user_id,
            handoff_id,
            sets={
                "#status": ("status", "done"),
                "#outputText": (PROMPT_FIELD_OUTPUT_TEXT, text),
                "#outputHash": (PROMPT_FIELD_OUTPUT_HASH, context_hash(text)),
                "#outputTokens": (PROMPT_FIELD_OUTPUT_TOKENS, max(0, int(output_tokens or 0))),
            },
            condition="#status = :from0",
            condition_values={":from0": "running"},
            remove=(PROMPT_FIELD_STOPPED_REASON,),
        )

    def stop(self, user_id: str, handoff_id: str, reason: PromptStopReason) -> bool:
        """``running`` → ``stopped`` with ``stoppedReason``. False if the step isn't running."""
        if reason not in PROMPT_STOP_REASONS:
            raise ValueError(f"not a prompt step stop reason: {reason}")
        return self._update(
            user_id,
            handoff_id,
            sets={"#status": ("status", "stopped"), "#stoppedReason": (PROMPT_FIELD_STOPPED_REASON, reason)},
            condition="#status = :from0",
            condition_values={":from0": "running"},
        )

    def _update(
        self,
        user_id: str,
        handoff_id: str,
        *,
        sets: Dict[str, Any],
        condition: str,
        condition_values: Dict[str, Any],
        condition_names: Optional[Dict[str, str]] = None,
        remove: tuple = (),
    ) -> bool:
        if self._table is None:
            from apis.shared.handoff.store import HandoffStoreUnavailable

            raise HandoffStoreUnavailable("DYNAMODB_SESSIONS_METADATA_TABLE_NAME is not configured")
        names: Dict[str, str] = {"#updatedAt": PROMPT_FIELD_UPDATED_AT, **(condition_names or {})}
        values: Dict[str, Any] = {":now": datetime.now(timezone.utc).isoformat(), **condition_values}
        clauses: List[str] = ["#updatedAt = :now"]
        for placeholder, (attribute, value) in sets.items():
            names[placeholder] = attribute
            values[f":{attribute}"] = value
            clauses.append(f"{placeholder} = :{attribute}")
        expression = "SET " + ", ".join(clauses)
        if remove:
            for attribute in remove:
                names[f"#{attribute}"] = attribute
            expression += " REMOVE " + ", ".join(f"#{attribute}" for attribute in remove)
        try:
            self._table.update_item(
                Key={"PK": f"USER#{user_id}", "SK": f"{_SK_PREFIX}{handoff_id}"},
                UpdateExpression=expression,
                ConditionExpression=f"attribute_exists(SK) AND {condition}",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                return False
            raise
        return True


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None) or {}
    return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


_store: Optional[PromptStepStore] = None


def get_prompt_step_store() -> PromptStepStore:
    """Get or create the process-global prompt step store."""
    global _store
    if _store is None:
        _store = PromptStepStore()
    return _store


__all__ = [
    "CUT_OFF_MARKER",
    "PromptStepStore",
    "mark_cut_off",
    "WORKFLOW_PROMPT_MAX_STEPS",
    "WorkflowPromptRequest",
    "get_prompt_step_store",
]
