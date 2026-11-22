"""``WFCHK#`` rows: what a workflow checklist step found, one row per field set (workflow-designer §22.3).

A checklist step runs named field sets over the run's files, one model call per set, and resolves
every value through ``source_provenance``. Two things come of it:

- **The model-facing result** (values and states only, one table per set) is the step's
  ``outputText`` on its ``HANDOFF#`` row. That is what collect and ``prepare`` carry as the step's
  text part, and what a later step reads.
- **The display rows** keep what a reviewer needs and the model never sees: each value's quote,
  locator and method. They live here.

The Runtime's ``workflow_prompt`` invocation (PR-D.7b) writes them with :func:`put_checklist_set`,
**before** the step's ``done`` write, so a reader that sees ``done`` finds every set's row. The run
read and the ``workflowRuns`` replay on ``GET /messages`` read them with :func:`read_checklist_sets`
and attach each step's rows to it.

STORAGE
-------
Reuses the ``sessions-metadata`` table and its ``SessionLookupIndex`` GSI, as ``EXTR#`` rows do, so
this needs **zero new infra**::

    PK:     USER#<user_id>
    SK:     WFCHK#<run_id>#<step_id>#<field_set>
    GSI_PK: SESSION#<source_session_id>    (SessionLookupIndex)
    GSI_SK: WFCHK#<run_id>#<step_id>#<field_set>

One row per (step, set), so a step of 16 sets is 16 small items, each well inside the 400 KB limit,
and running a set again overwrites its own row. The GSI sort key leads with the run, so one run's
rows are one ``begins_with`` query; a reader sorts them into the step's authored order by
``setIndex``. Rows carry quoted passages from the user's own document, so values and quotes are
clamped, and a row that would still not fit drops its quotes rather than fail the write, exactly as
``EXTR#`` does. They expire with the run (365 days, the run row's TTL) and go with the session
delete cascade beside the run's own rows (``ChecklistRowStore.delete_for_session``).

Nothing here is on a turn's path.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, FrozenSet, List, Literal, Mapping, Optional, Sequence, Union

from pydantic import BaseModel, ConfigDict, Field

try:  # boto3 is absent in some local-dev setups
    import boto3
    from boto3.dynamodb.conditions import Key
except ImportError:  # pragma: no cover - exercised only without boto3
    boto3 = None
    Key = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

SK_PREFIX = "WFCHK#"

#: A set's own outcome. ``error`` is a set that failed on its own (no tool call twice, a call that
#: failed); it never stops the step's other sets.
ChecklistSetStatus = Literal["done", "error"]
CHECKLIST_SET_STATUSES: FrozenSet[str] = frozenset({"done", "error"})

#: ``source_provenance``'s four states, as ``EXTR#`` rows carry them.
ProvenanceState = Literal["supported", "quote_unsupported", "unassessed", "unverified"]

#: The run row's TTL, so a step's display rows last exactly as long as the run that shows them.
_TTL_DAYS = 365

#: Clamps on one stored value and quote, as ``EXTR#``'s.
MAX_VALUE_CHARS = 1000
MAX_QUOTE_CHARS = 1000
#: Fields kept per set. A field set holds at most 14 (``extraction.field_sets.MAX_FIELDS``); this
#: guards the row against a caller that did not parse one.
MAX_FIELDS = 40
#: Above this, the quotes are dropped before the write: well under DynamoDB's 400 KB, with room
#: for the rest of the item and attribute-name overhead.
MAX_ITEM_CHARS = 300_000

_MAX_ERROR_CHARS = 64


class ChecklistLocator(BaseModel):
    """Where a quote was found: the unit the document has, and how sure the position is."""

    model_config = ConfigDict(populate_by_name=True)

    kind: str
    value: int
    #: Always present: an absent precision reads as exact, which is how a guess becomes ``p. 234``.
    precision: str


class ChecklistField(BaseModel):
    """One field of one set, as a reviewer reads it."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    title: str = ""
    type: str = ""
    value: Optional[str] = Field(None, description="The value as the Runtime coerced it, for display; null when not found")
    state: str = Field("unverified", description="One of supported, quote_unsupported, unassessed, unverified")
    method: str = ""
    quote: Optional[str] = None
    locator: Optional[ChecklistLocator] = None


class ChecklistSetRow(BaseModel):
    """One field set's row: what the step found for it, on the wire as the SPA reads it."""

    model_config = ConfigDict(populate_by_name=True)

    run_id: str = Field(..., alias="runId")
    step_id: str = Field(..., alias="stepId")
    field_set: str = Field(..., alias="fieldSet")
    set_index: int = Field(0, alias="setIndex", description="Its place in the step's fieldSets")
    field_set_title: str = Field("", alias="fieldSetTitle")
    field_set_version: str = Field("", alias="fieldSetVersion")
    model_id: str = Field("", alias="modelId")
    status: ChecklistSetStatus
    error: Optional[str] = Field(None, description="A short reason code when status is error")
    fields: List[ChecklistField] = Field(default_factory=list)
    quotes_omitted: bool = Field(False, alias="quotesOmitted", description="The quotes were dropped to fit the item limit")
    created_at: str = Field("", alias="createdAt")


FieldInput = Union[ChecklistField, Mapping[str, Any]]


class ChecklistRowStore:
    """Per-user store of checklist steps' ``WFCHK#`` rows, in sessions-metadata."""

    def __init__(self, table: Any = None) -> None:
        self._table = table
        if self._table is not None or boto3 is None:
            return
        table_name = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
        if not table_name:
            return
        try:
            self._table = boto3.resource("dynamodb").Table(table_name)
        except Exception:  # noqa: BLE001 - dev without AWS creds
            logger.warning("checklist rows: DynamoDB unavailable; rows are not kept", exc_info=True)
            self._table = None

    @property
    def enabled(self) -> bool:
        return self._table is not None

    def put(
        self,
        *,
        user_id: str,
        session_id: str,
        run_id: str,
        step_id: str,
        field_set: str,
        set_index: int,
        field_set_version: str,
        status: str,
        fields: Sequence[FieldInput],
        model_id: str = "",
        field_set_title: str = "",
        error: Optional[str] = None,
    ) -> bool:
        """Write one set's row, replacing any earlier one. ``True`` when written; never raises.

        A failed write costs the display only: the step's answer is on its ``HANDOFF#`` row, which
        is what a result carries.
        """
        if self._table is None or not (user_id and session_id and run_id and step_id and field_set):
            return False
        if status not in CHECKLIST_SET_STATUSES:
            logger.warning("checklist rows: refusing a row with an unknown status")
            return False
        try:
            item = _item(
                user_id=user_id,
                session_id=session_id,
                run_id=run_id,
                step_id=step_id,
                field_set=field_set,
                set_index=set_index,
                field_set_version=field_set_version,
                status=status,
                fields=fields,
                model_id=model_id,
                field_set_title=field_set_title,
                error=error,
            )
            self._table.put_item(Item=item)
            return True
        except Exception:  # noqa: BLE001 - display rows are best-effort
            logger.warning("checklist rows: write failed (run=%s, step=%s)", run_id, step_id, exc_info=True)
            return False

    def read(self, *, user_id: str, session_id: str, run_id: Optional[str] = None) -> List[ChecklistSetRow]:
        """This user's rows on a session (one run's when ``run_id`` is given), in step order. Never raises.

        Queried off the session GSI then re-checked on ``userId``, because a session id alone does
        not name a conversation, and these rows carry document text.
        """
        if self._table is None or not session_id:
            return []
        prefix = f"{SK_PREFIX}{run_id}#" if run_id else SK_PREFIX
        try:
            items: List[Dict[str, Any]] = []
            kwargs: Dict[str, Any] = {
                "IndexName": "SessionLookupIndex",
                "KeyConditionExpression": Key("GSI_PK").eq(f"SESSION#{session_id}") & Key("GSI_SK").begins_with(prefix),
            }
            while True:
                resp = self._table.query(**kwargs)
                items.extend(resp.get("Items", []))
                lek = resp.get("LastEvaluatedKey")
                if not lek:
                    break
                kwargs["ExclusiveStartKey"] = lek
        except Exception:  # noqa: BLE001 - a read that fails costs only the tables
            logger.warning("checklist rows: query failed", exc_info=True)
            return []
        rows: List[ChecklistSetRow] = []
        for item in items:
            if item.get("userId") != user_id:
                continue
            row = _row(item)
            if row is not None:
                rows.append(row)
        rows.sort(key=lambda r: (r.run_id, r.step_id, r.set_index, r.field_set))
        return rows

    def delete_for_session(self, user_id: str, session_id: str) -> int:
        """Delete this user's rows on a source conversation (the delete cascade). Returns the count."""
        if self._table is None:
            return 0
        deleted = 0
        for row in self.read(user_id=user_id, session_id=session_id):
            try:
                self._table.delete_item(
                    Key={"PK": f"USER#{user_id}", "SK": f"{SK_PREFIX}{row.run_id}#{row.step_id}#{row.field_set}"}
                )
                deleted += 1
            except Exception:  # noqa: BLE001 - best-effort, like the sibling stores
                logger.warning("checklist rows: delete failed", exc_info=True)
        return deleted


def _item(
    *,
    user_id: str,
    session_id: str,
    run_id: str,
    step_id: str,
    field_set: str,
    set_index: int,
    field_set_version: str,
    status: str,
    fields: Sequence[FieldInput],
    model_id: str,
    field_set_title: str,
    error: Optional[str],
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    key = f"{SK_PREFIX}{run_id}#{step_id}#{field_set}"
    item: Dict[str, Any] = {
        "PK": f"USER#{user_id}",
        "SK": key,
        "GSI_PK": f"SESSION#{session_id}",
        "GSI_SK": key,
        "userId": user_id,
        "sessionId": session_id,
        "runId": run_id,
        "stepId": step_id,
        "fieldSet": str(field_set)[:120],
        "setIndex": max(int(set_index), 0),
        "fieldSetTitle": str(field_set_title or "")[:200],
        "fieldSetVersion": str(field_set_version or "")[:40],
        "modelId": str(model_id or "")[:160],
        "status": status,
        "fields": [_clamp_field(f) for f in list(fields)[:MAX_FIELDS]],
        "createdAt": now.isoformat(),
        "ttl": int((now + timedelta(days=_TTL_DAYS)).timestamp()),
    }
    if error and status == "error":
        item["error"] = str(error)[:_MAX_ERROR_CHARS]
    if _rough_size(item) > MAX_ITEM_CHARS:
        # Names, values and states survive; the quotes go. A row that fails to write loses the
        # states too, which are the part a reviewer most needs.
        logger.info("checklist rows: dropping quotes to fit the item limit (run=%s, step=%s)", run_id, step_id)
        for field in item["fields"]:
            field.pop("quote", None)
        item["quotesOmitted"] = True
    return item


def _clamp_field(field: FieldInput) -> Dict[str, Any]:
    data = field.model_dump(by_alias=True, exclude_none=True) if isinstance(field, BaseModel) else dict(field)
    stored: Dict[str, Any] = {
        "name": str(data.get("name", ""))[:120],
        "title": str(data.get("title", "") or "")[:200],
        "type": str(data.get("type", "") or "")[:20],
        "state": str(data.get("state", "") or "unverified")[:32],
        "method": str(data.get("method", "") or "")[:32],
    }
    value = data.get("value")
    if value is not None:
        stored["value"] = str(value)[:MAX_VALUE_CHARS]
    quote = data.get("quote")
    if quote:
        stored["quote"] = str(quote)[:MAX_QUOTE_CHARS]
    locator = data.get("locator")
    if isinstance(locator, BaseModel):
        locator = locator.model_dump()
    if isinstance(locator, Mapping):
        stored["locator"] = {
            "kind": str(locator.get("kind", ""))[:20],
            "value": int(locator.get("value") or 0),
            "precision": str(locator.get("precision", ""))[:20],
        }
    return stored


def _row(item: Mapping[str, Any]) -> Optional[ChecklistSetRow]:
    try:
        fields = []
        for raw in item.get("fields") or []:
            field = dict(raw)
            locator = field.get("locator")
            if isinstance(locator, Mapping):
                field["locator"] = {**locator, "value": int(locator.get("value") or 0)}
            fields.append(ChecklistField.model_validate(field))
        return ChecklistSetRow(
            run_id=str(item.get("runId", "")),
            step_id=str(item.get("stepId", "")),
            field_set=str(item.get("fieldSet", "")),
            set_index=int(item.get("setIndex") or 0),
            field_set_title=str(item.get("fieldSetTitle", "")),
            field_set_version=str(item.get("fieldSetVersion", "")),
            model_id=str(item.get("modelId", "")),
            status=str(item.get("status", "error")),
            error=str(item["error"]) if item.get("error") else None,
            fields=fields,
            quotes_omitted=bool(item.get("quotesOmitted", False)),
            created_at=str(item.get("createdAt", "")),
        )
    except Exception:  # noqa: BLE001 - one bad row never hides the rest
        logger.warning("checklist rows: skipping a row that doesn't read", exc_info=True)
        return None


def _rough_size(item: Mapping[str, Any]) -> int:
    try:
        return len(json.dumps(item, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001
        return 0


_store: Optional[ChecklistRowStore] = None


def get_checklist_row_store() -> ChecklistRowStore:
    """Get or create the process-global store."""
    global _store
    if _store is None:
        _store = ChecklistRowStore()
    return _store


def put_checklist_set(
    *,
    user_id: str,
    session_id: str,
    run_id: str,
    step_id: str,
    field_set: str,
    set_index: int,
    field_set_version: str,
    status: str,
    fields: Sequence[FieldInput],
    model_id: str = "",
    field_set_title: str = "",
    error: Optional[str] = None,
    store: Optional[ChecklistRowStore] = None,
) -> bool:
    """Write one field set's row for a checklist step (the Runtime, PR-D.7b). Never raises.

    ``session_id`` is the run's **source** session. ``set_index`` is the set's place in the step's
    ``fieldSets``. Call it for every set before the step's ``done`` write.
    """
    return (store or get_checklist_row_store()).put(
        user_id=user_id,
        session_id=session_id,
        run_id=run_id,
        step_id=step_id,
        field_set=field_set,
        set_index=set_index,
        field_set_version=field_set_version,
        status=status,
        fields=fields,
        model_id=model_id,
        field_set_title=field_set_title,
        error=error,
    )


def read_checklist_sets(
    *,
    user_id: str,
    session_id: str,
    run_id: Optional[str] = None,
    store: Optional[ChecklistRowStore] = None,
) -> List[ChecklistSetRow]:
    """The caller's checklist rows on a source session, one run's when ``run_id`` is given. Never raises."""
    return (store or get_checklist_row_store()).read(user_id=user_id, session_id=session_id, run_id=run_id)


def rows_by_step(rows: Sequence[ChecklistSetRow]) -> Dict[str, List[ChecklistSetRow]]:
    """``{step_id: rows}`` in each step's authored order (the rows come sorted)."""
    grouped: Dict[str, List[ChecklistSetRow]] = {}
    for row in rows:
        grouped.setdefault(row.step_id, []).append(row)
    return grouped


__all__ = [
    "CHECKLIST_SET_STATUSES",
    "MAX_FIELDS",
    "MAX_ITEM_CHARS",
    "MAX_QUOTE_CHARS",
    "MAX_VALUE_CHARS",
    "SK_PREFIX",
    "ChecklistField",
    "ChecklistLocator",
    "ChecklistRowStore",
    "ChecklistSetRow",
    "ChecklistSetStatus",
    "ProvenanceState",
    "get_checklist_row_store",
    "put_checklist_set",
    "read_checklist_sets",
    "rows_by_step",
]
