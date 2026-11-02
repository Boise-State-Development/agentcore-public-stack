"""``HANDOFF#`` rows in sessions-metadata (``docs/specs/agent-handoffs-and-consults.md`` §4.1).

Reuses the sessions-metadata table and its ``SessionLookupIndex`` GSI, like ``TSUM#``, so it needs
**no new infra**::

    PK:     USER#<user_id>
    SK:     HANDOFF#<handoff_id>
    GSI_PK: SESSION#<source_session_id>     (SessionLookupIndex)
    GSI_SK: HANDOFF#<created_at>

Owner scoping is the partition key: a row is only ever read or written under the caller's own
``USER#``, so another user's handoff id is simply absent. A list off the GSI re-checks ``userId``,
because a session id alone does not name a conversation (CLAUDE.md, "one session can be served by
more than one agent").

Every status change is a conditional update against ``TRANSITIONS``, so a ``done`` row stays done.
Acceptance (``accept``) is narrower still: it moves only out of ``open``, so two sends of one
handoff cannot both start a conversation.

Rows carry the user's request text, so they expire with the conversation (365 days, the
conversation retention live on dev) and are deleted by the session delete cascade.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:  # boto3 is absent in some local-dev setups
    import boto3
    from boto3.dynamodb.conditions import Key
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - exercised only without boto3
    boto3 = None
    Key = None  # type: ignore[assignment]
    ClientError = Exception  # type: ignore[assignment, misc]

from apis.shared.handoff.models import (
    HANDOFF_DISPLAY_MAX_CHARS,
    Handoff,
    HandoffStatus,
    WaitingOn,
    sources_of,
)

logger = logging.getLogger(__name__)

_HANDOFF_TTL_DAYS = 365
_SK_PREFIX = "HANDOFF#"
_KEY_ATTRS = ("PK", "SK", "GSI_PK", "GSI_SK", "ttl")

#: The fields a status change may also set, by attribute name.
_TRANSITION_FIELDS = (
    "targetSessionId",
    "contextEdited",
    "contextRemoved",
    "statusLine",
    "waitingOn",
    "stoppedReason",
)
_DISPLAY_FIELDS = ("statusLine", "stoppedReason")


class HandoffStoreUnavailable(RuntimeError):
    """The sessions-metadata table isn't configured, so a handoff can't be recorded."""


class HandoffStore:
    """Per-user store of handoff rows."""

    def __init__(self, table: Any = None) -> None:
        self._table = table
        if self._table is not None or boto3 is None:
            return
        table_name = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
        if not table_name:
            return
        try:
            # The process-wide cached resource, not a fresh boto3.resource: constructing one costs
            # ~47ms on a throttled Runtime container (turn-path spec §2B), and a handoff target's
            # first turn usually lands on a fresh process.
            from apis.shared.aws_clients import get_dynamodb_table

            self._table = get_dynamodb_table(table_name)
        except Exception:  # noqa: BLE001 - dev without AWS creds
            logger.warning("handoff store: DynamoDB unavailable", exc_info=True)
            self._table = None

    @property
    def enabled(self) -> bool:
        return self._table is not None

    def create(self, handoff: Handoff) -> Handoff:
        """Write a new row. Raises if the table is missing or the id is already taken."""
        table = self._require_table()
        item = handoff.model_dump(by_alias=True, exclude_none=True)
        item.update(
            {
                "PK": f"USER#{handoff.user_id}",
                "SK": f"{_SK_PREFIX}{handoff.handoff_id}",
                "GSI_PK": f"SESSION#{handoff.session_id}",
                "GSI_SK": f"{_SK_PREFIX}{handoff.created_at}",
                "ttl": int((datetime.now(timezone.utc) + timedelta(days=_HANDOFF_TTL_DAYS)).timestamp()),
            }
        )
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(SK)")
        return handoff

    def get(self, user_id: str, handoff_id: str) -> Optional[Handoff]:
        """The caller's row, or ``None``. Another user's id reads as absent."""
        table = self._require_table()
        resp = table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"{_SK_PREFIX}{handoff_id}"})
        item = resp.get("Item")
        return _from_item(item) if item else None

    def list_for_session(self, user_id: str, session_id: str) -> List[Handoff]:
        """This user's handoffs from one source conversation, oldest first."""
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
            logger.warning("handoff store: query failed", exc_info=True)
            return []
        return [_from_item(item) for item in items if item.get("userId") == user_id]

    def accept(
        self,
        user_id: str,
        handoff_id: str,
        *,
        target_session_id: str,
        context_edited: bool,
        context_removed: bool,
    ) -> Optional[Handoff]:
        """The target's first turn takes the handoff: ``open`` → ``running``, exactly once.

        ``None`` when it was already accepted, dismissed, or isn't the caller's. This is the
        no-double-send guard, so it is a condition on the write, not a read before it.
        """
        return self._move(
            user_id,
            handoff_id,
            "running",
            ["open"],
            {
                "targetSessionId": target_session_id,
                "contextEdited": context_edited,
                "contextRemoved": context_removed,
            },
        )

    def transition(
        self,
        user_id: str,
        handoff_id: str,
        status: HandoffStatus,
        **fields: Any,
    ) -> Optional[Handoff]:
        """Move a row to ``status`` if ``TRANSITIONS`` allows it from where it is.

        ``fields`` are camelCase attributes from ``_TRANSITION_FIELDS``; a ``None`` value is left
        unset. Returns the updated row, or ``None`` when the row is missing, owned by someone else,
        or in a status the move isn't allowed from. The caller reads the row again if it needs to
        tell those apart. Leaving ``open`` for ``running`` goes through ``accept``.
        """
        unknown = set(fields) - set(_TRANSITION_FIELDS)
        if unknown:
            raise ValueError(f"not a transition field: {sorted(unknown)}")
        allowed = sorted(sources_of(status) - ({"open"} if status == "running" else set()))
        return self._move(user_id, handoff_id, status, allowed, fields)

    def _move(
        self,
        user_id: str,
        handoff_id: str,
        status: HandoffStatus,
        allowed: List[str],
        fields: Dict[str, Any],
    ) -> Optional[Handoff]:
        table = self._require_table()
        if not allowed:
            return None

        names = {"#status": "status", "#updatedAt": "updatedAt"}
        values: Dict[str, Any] = {":to": status, ":now": datetime.now(timezone.utc).isoformat()}
        sets = ["#status = :to", "#updatedAt = :now"]
        for name in _TRANSITION_FIELDS:
            value = fields.get(name)
            if value is None:
                continue
            if name in _DISPLAY_FIELDS:
                value = str(value)[:HANDOFF_DISPLAY_MAX_CHARS]
            elif name == "waitingOn":
                value = WaitingOn.model_validate(value).model_dump()
                value["name"] = value["name"][:HANDOFF_DISPLAY_MAX_CHARS]
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
                Key={"PK": f"USER#{user_id}", "SK": f"{_SK_PREFIX}{handoff_id}"},
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
        """Delete this user's handoffs from a source conversation. Best-effort; returns the count."""
        if self._table is None:
            return 0
        deleted = 0
        for handoff in self.list_for_session(user_id, session_id):
            try:
                self._table.delete_item(
                    Key={"PK": f"USER#{user_id}", "SK": f"{_SK_PREFIX}{handoff.handoff_id}"}
                )
                deleted += 1
            except ClientError:
                logger.warning("handoff store: delete failed", exc_info=True)
        return deleted

    def _require_table(self) -> Any:
        if self._table is None:
            raise HandoffStoreUnavailable("DYNAMODB_SESSIONS_METADATA_TABLE_NAME is not configured")
        return self._table


def _from_item(item: Dict[str, Any]) -> Handoff:
    data = {k: v for k, v in item.items() if k not in _KEY_ATTRS}
    for key in ("sourceMessageIndex", "excerptTokens"):
        if key in data:
            data[key] = int(data[key])
    turns = data.get("turnsCovered")
    if isinstance(turns, dict):
        data["turnsCovered"] = {k: int(v) for k, v in turns.items()}
    return Handoff.model_validate(data)


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None) or {}
    return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


_store: Optional[HandoffStore] = None


def get_handoff_store() -> HandoffStore:
    """Get or create the process-global handoff store."""
    global _store
    if _store is None:
        _store = HandoffStore()
    return _store


__all__ = ["HandoffStore", "HandoffStoreUnavailable", "get_handoff_store"]
