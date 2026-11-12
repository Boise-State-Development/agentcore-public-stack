"""``DELIVERY#`` rows: text from one of the user's conversations, landed in another (spec §4.8, PR-0.4).

A delivery sits on its **receiving** conversation, in sessions-metadata, under its owner, with the
``SessionLookupIndex`` GSI pointing at the receiving session, like ``HANDOFF#``. No new infra::

    PK:     USER#<user_id>
    SK:     DELIVERY#<delivery_id>
    GSI_PK: SESSION#<to_session_id>         (SessionLookupIndex)
    GSI_SK: DELIVERY#<created_at>

States: ``pending`` → ``attached`` | ``dismissed``. A delivery never starts a turn and never enters a
running one; it waits on the receiving composer until the user sends.

**Exactly once.** ``attach`` is one conditional update, ``pending`` → ``attached`` on
``(owner, receiving session)``, returning the row. Two tabs sending the same card both try it, and
only the one whose write succeeds gets the row back, so only it carries the text. No read comes
first, so a send that carries deliveries pays one round trip per card (at most
``DELIVERY_MAX_PER_TURN``, issued together) and nothing else before the first token.

Rows carry conversation text, so they expire with the conversation (365 days) and are deleted by
the receiving conversation's delete cascade.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

try:  # boto3 is absent in some local-dev setups
    from boto3.dynamodb.conditions import Key
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - exercised only without boto3
    Key = None  # type: ignore[assignment]
    ClientError = Exception  # type: ignore[assignment, misc]

from apis.shared.handoff.excerpt import CHARS_PER_TOKEN

logger = logging.getLogger(__name__)

#: A returned result's budget (spec §4.3). A delivery's text, edited or not, never exceeds it.
SEND_BACK_MAX_TOKENS = 2_000
SEND_BACK_MAX_CHARS = SEND_BACK_MAX_TOKENS * CHARS_PER_TOKEN
#: Artifact references one delivery may carry (§4.8).
DELIVERY_MAX_ARTIFACTS = 5
#: Delivery blocks one turn carries; the rest stay pending for the next send (§4.8).
DELIVERY_MAX_PER_TURN = 3

DeliveryOrigin = Literal["user", "agent"]
DeliveryStatus = Literal["pending", "attached", "dismissed"]

_TTL_DAYS = 365
_SK_PREFIX = "DELIVERY#"
_KEY_ATTRS = ("PK", "SK", "GSI_PK", "GSI_SK", "ttl")
_TRUNCATED = "[truncated]"


class DeliveryArtifact(BaseModel):
    """A reference to an artifact the sending conversation made, pinned at the version sent."""

    model_config = ConfigDict(populate_by_name=True)

    artifact_id: str = Field(..., alias="artifactId")
    version: int
    title: str = ""
    content_type: str = Field("", alias="contentType")


class Delivery(BaseModel):
    """One ``DELIVERY#<deliveryId>`` row, keyed under its owner and indexed by its receiving session."""

    model_config = ConfigDict(populate_by_name=True)

    delivery_id: str = Field(..., alias="deliveryId")
    user_id: str = Field(..., alias="userId")
    to_session_id: str = Field(..., alias="toSessionId", description="The receiving conversation")
    from_session_id: str = Field(..., alias="fromSessionId")
    from_title: str = Field("", alias="fromTitle", description="The sender's title when created; the block names it")
    handoff_id: Optional[str] = Field(None, alias="handoffId")
    origin: DeliveryOrigin = "user"
    text: str = ""
    text_hash: str = Field("", alias="textHash")
    artifacts: List[DeliveryArtifact] = Field(default_factory=list)
    status: DeliveryStatus = "pending"
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")
    attached_via: Optional[Literal["turn", "steer", "wake"]] = Field(None, alias="attachedVia")
    attached_at: Optional[str] = Field(None, alias="attachedAt")
    edited: Optional[bool] = None
    hop: int = 0


class DeliveryView(BaseModel):
    """A pending delivery as the receiving composer shows it (``deliveries`` on ``GET /messages``)."""

    model_config = ConfigDict(populate_by_name=True)

    delivery_id: str = Field(..., alias="deliveryId")
    from_session_id: str = Field(..., alias="fromSessionId")
    from_title: str = Field(..., alias="fromTitle")
    handoff_id: Optional[str] = Field(None, alias="handoffId")
    origin: DeliveryOrigin
    text: str
    artifacts: List[DeliveryArtifact]
    created_at: str = Field(..., alias="createdAt")

    @classmethod
    def from_row(cls, row: Delivery) -> "DeliveryView":
        return cls(
            delivery_id=row.delivery_id,
            from_session_id=row.from_session_id,
            from_title=row.from_title,
            handoff_id=row.handoff_id,
            origin=row.origin,
            text=row.text,
            artifacts=row.artifacts,
            created_at=row.created_at,
        )


def cap_delivery_text(text: str) -> str:
    """A delivery's text as stored or attached: normalized, then cut to ``SEND_BACK_MAX_CHARS``.

    Cut at the last paragraph break that fits (a line break, else a space), so the model never sees
    half a sentence it can't tell was cut, and marked ``[truncated]`` (§4.3).
    """
    from apis.shared.handoff.context import normalize_context_text

    text = normalize_context_text(text)
    if len(text) <= SEND_BACK_MAX_CHARS:
        return text
    room = SEND_BACK_MAX_CHARS - len(_TRUNCATED) - 2
    head = text[:room]
    for sep in ("\n\n", "\n", " "):
        cut = head.rfind(sep)
        if cut >= room // 2:
            head = head[:cut]
            break
    return f"{head.rstrip()}\n\n{_TRUNCATED}"


class DeliveryStore:
    """Per-user store of ``DELIVERY#`` rows. Shares the handoff store's table."""

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
            logger.warning("delivery store: DynamoDB unavailable", exc_info=True)
            self._table = None

    @property
    def enabled(self) -> bool:
        return self._table is not None

    def create(self, delivery: Delivery) -> Delivery:
        """Write a new ``pending`` row. Raises if the table is missing or the id is taken."""
        table = self._require_table()
        item = delivery.model_dump(by_alias=True, exclude_none=True)
        item.update(
            {
                "PK": f"USER#{delivery.user_id}",
                "SK": f"{_SK_PREFIX}{delivery.delivery_id}",
                "GSI_PK": f"SESSION#{delivery.to_session_id}",
                "GSI_SK": f"{_SK_PREFIX}{delivery.created_at}",
                "ttl": int((datetime.now(timezone.utc) + timedelta(days=_TTL_DAYS)).timestamp()),
            }
        )
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(SK)")
        return delivery

    def get(self, user_id: str, delivery_id: str) -> Optional[Delivery]:
        """The caller's row, or ``None``. Another user's id reads as absent."""
        table = self._require_table()
        resp = table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"{_SK_PREFIX}{delivery_id}"})
        item = resp.get("Item")
        return _from_item(item) if item else None

    def list_pending(self, user_id: str, to_session_id: str) -> List[Delivery]:
        """This user's pending deliveries into one conversation, oldest first. Never raises."""
        if self._table is None:
            return []
        items: List[Dict[str, Any]] = []
        kwargs: Dict[str, Any] = {
            "IndexName": "SessionLookupIndex",
            "KeyConditionExpression": Key("GSI_PK").eq(f"SESSION#{to_session_id}")
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
            logger.warning("delivery store: query failed", exc_info=True)
            return []
        # The GSI is keyed by session alone: re-check the owner (a session id is not a conversation).
        rows = [_from_item(item) for item in items if item.get("userId") == user_id]
        return [row for row in rows if row.status == "pending"]

    def attach(self, user_id: str, delivery_id: str, to_session_id: str) -> Optional[Delivery]:
        """``pending`` → ``attached`` for this owner and receiving session, exactly once.

        Returns the row as it now is, or ``None`` when it is missing, another user's, for another
        conversation, or no longer pending. The condition on the write is the guard; nothing is read
        first.
        """
        table = self._require_table()
        now = datetime.now(timezone.utc).isoformat()
        try:
            resp = table.update_item(
                Key={"PK": f"USER#{user_id}", "SK": f"{_SK_PREFIX}{delivery_id}"},
                UpdateExpression="SET #status = :attached, attachedVia = :via, attachedAt = :now, updatedAt = :now",
                ConditionExpression="attribute_exists(SK) AND #status = :pending AND toSessionId = :to",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":attached": "attached",
                    ":pending": "pending",
                    ":via": "turn",
                    ":now": now,
                    ":to": to_session_id,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                return None
            raise
        return _from_item(resp["Attributes"])

    def mark_edited(self, user_id: str, delivery_id: str) -> None:
        """Record that the card was edited before it was attached. Off the turn path."""
        table = self._require_table()
        table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": f"{_SK_PREFIX}{delivery_id}"},
            UpdateExpression="SET edited = :true",
            ConditionExpression="attribute_exists(SK)",
            ExpressionAttributeValues={":true": True},
        )

    def dismiss(self, user_id: str, delivery_id: str, to_session_id: str) -> Optional[Delivery]:
        """``pending`` → ``dismissed`` (the card's Remove). ``None`` if it is not pending here."""
        table = self._require_table()
        now = datetime.now(timezone.utc).isoformat()
        try:
            resp = table.update_item(
                Key={"PK": f"USER#{user_id}", "SK": f"{_SK_PREFIX}{delivery_id}"},
                UpdateExpression="SET #status = :dismissed, updatedAt = :now",
                ConditionExpression="attribute_exists(SK) AND #status = :pending AND toSessionId = :to",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":dismissed": "dismissed",
                    ":pending": "pending",
                    ":now": now,
                    ":to": to_session_id,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if _is_conditional_failure(exc):
                return None
            raise
        return _from_item(resp["Attributes"])

    def delete_for_session(self, user_id: str, to_session_id: str) -> int:
        """Delete this user's deliveries into a conversation, whatever their status. Best-effort."""
        if self._table is None:
            return 0
        deleted = 0
        kwargs: Dict[str, Any] = {
            "IndexName": "SessionLookupIndex",
            "KeyConditionExpression": Key("GSI_PK").eq(f"SESSION#{to_session_id}")
            & Key("GSI_SK").begins_with(_SK_PREFIX),
        }
        try:
            while True:
                resp = self._table.query(**kwargs)
                for item in resp.get("Items", []):
                    if item.get("userId") != user_id:
                        continue
                    self._table.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
                    deleted += 1
                lek = resp.get("LastEvaluatedKey")
                if not lek:
                    break
                kwargs["ExclusiveStartKey"] = lek
        except ClientError:
            logger.warning("delivery store: delete failed", exc_info=True)
        return deleted

    def _require_table(self) -> Any:
        if self._table is None:
            from apis.shared.handoff.store import HandoffStoreUnavailable

            raise HandoffStoreUnavailable("DYNAMODB_SESSIONS_METADATA_TABLE_NAME is not configured")
        return self._table


def _from_item(item: Dict[str, Any]) -> Delivery:
    data = {k: v for k, v in item.items() if k not in _KEY_ATTRS}
    if "hop" in data:
        data["hop"] = int(data["hop"])
    artifacts = data.get("artifacts")
    if isinstance(artifacts, list):
        data["artifacts"] = [{**a, "version": int(a.get("version", 1))} for a in artifacts if isinstance(a, dict)]
    return Delivery.model_validate(data)


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None) or {}
    return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


_store: Optional[DeliveryStore] = None


def get_delivery_store() -> DeliveryStore:
    """Get or create the process-global delivery store."""
    global _store
    if _store is None:
        _store = DeliveryStore()
    return _store


__all__ = [
    "DELIVERY_MAX_ARTIFACTS",
    "DELIVERY_MAX_PER_TURN",
    "Delivery",
    "DeliveryArtifact",
    "DeliveryOrigin",
    "DeliveryStatus",
    "DeliveryStore",
    "DeliveryView",
    "SEND_BACK_MAX_CHARS",
    "SEND_BACK_MAX_TOKENS",
    "cap_delivery_text",
    "get_delivery_store",
]
