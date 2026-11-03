"""DynamoDB persistence for device-authorization grants.

Two access patterns, both of which must be a single-key read:

* **Poll** — the CLI presents its ``device_code``; we look up by
  ``device_code_hash``.
* **Browser approval** — the human types a ``user_code``; we look up by that.

Rather than add a GSI for the second, each grant writes a small **pointer
item** keyed by the user code whose only payload is the grant's hash. The
browser leg resolves pointer → grant with two ``GetItem`` calls. A GSI would
also work, but it would be eventually consistent, and the browser approval
happens seconds after the grant is created — precisely the window where a
stale index read would report "unknown code" for a code the user is looking
at. The pointer item is strongly consistent by construction.

Storage layout (both items carry ``ttl``, so DynamoDB reaps them together)::

    Grant item
        PK = DEVICE-GRANT#<device_code_hash>, SK = META
        attrs: user_code, status, created_at, expires_at, ttl,
               session_id?, user_id?, approved_at?, claimed_at?,
               last_polled_at?, poll_count

    Pointer item
        PK = DEVICE-USERCODE#<user_code>, SK = META
        attrs: device_code_hash, ttl

The pair is written in a single ``TransactWriteItems`` so a partial create is
impossible: an orphaned grant could never be approved (the user code would not
resolve) and a dangling pointer would resolve to nothing. Both fail closed, but
neither is a state worth reasoning about later.

**Table choice.** These items live in the BFF sessions table by default,
keyed by prefixes no session path reads (sessions key ``SESSION#<id>``), the
same tactic ``apis.shared.harness.grants`` uses for headless-run grants. The
classification argument is strictly easier here than it is there: that module
stores Cognito refresh tokens, whereas a device grant holds a hash and a
``session_id`` — sealing a session value needs the cookie codec's key from
Secrets Manager, so this table is not a credential store. Reusing the table
means no new IAM grants and no infrastructure deploy before the feature works.
Set ``DEVICE_GRANTS_TABLE_NAME`` to move these items to a dedicated table
without a code change.

**Expiry is the caller's business.** Unlike ``SessionRepository.get``, the
readers here do *not* hide rows whose ``expires_at`` has passed. RFC 8628
clients need ``expired_token`` distinguished from an unknown grant, and only
the caller can tell those apart if the repository has already swallowed the
row. Use ``DeviceGrant.is_expired`` / ``is_claimable`` / ``is_approvable`` on
what you get back.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from .models import DeviceGrant, GrantStatus, normalise_user_code

logger = logging.getLogger(__name__)

#: Grant items. Distinct from ``SESSION#`` so no session access path sees them.
GRANT_PK_PREFIX = "DEVICE-GRANT#"

#: Pointer items resolving a typed user code to a grant hash.
USER_CODE_PK_PREFIX = "DEVICE-USERCODE#"

_SORT_KEY = "META"


class DeviceGrantRepository:
    """Async-shaped store for device grants.

    Every boto3 round-trip is offloaded with ``asyncio.to_thread``, matching
    ``SessionRepository``: boto3 is synchronous, and a blocking call on the
    uvicorn event loop stalls every other in-flight request. That matters more
    than usual here because the poll endpoint is, by design, called on a timer
    by every waiting CLI.
    """

    def __init__(self, table_name: Optional[str] = None) -> None:
        if table_name is None:
            table_name = os.environ.get("DEVICE_GRANTS_TABLE_NAME") or os.environ.get("BFF_SESSIONS_TABLE_NAME", "")

        self._table_name = table_name
        self._enabled = bool(table_name)

        if self._enabled:
            self._dynamodb = boto3.resource("dynamodb")
            self._table = self._dynamodb.Table(table_name)
            logger.info("DeviceGrantRepository initialized with table: %s", table_name)
        else:
            self._dynamodb = None
            self._table = None
            logger.debug("DeviceGrantRepository disabled — no device grants table configured")

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    # Keys and translation
    # ------------------------------------------------------------------

    @staticmethod
    def _grant_key(device_code_hash: str) -> dict:
        return {"PK": f"{GRANT_PK_PREFIX}{device_code_hash}", "SK": _SORT_KEY}

    @staticmethod
    def _pointer_key(user_code: str) -> dict:
        return {
            "PK": f"{USER_CODE_PK_PREFIX}{normalise_user_code(user_code)}",
            "SK": _SORT_KEY,
        }

    @staticmethod
    def _item_to_grant(item: dict) -> DeviceGrant:
        return DeviceGrant(
            device_code_hash=item["device_code_hash"],
            user_code=item["user_code"],
            status=GrantStatus(item["status"]),
            created_at=int(item["created_at"]),
            expires_at=int(item["expires_at"]),
            session_id=item.get("session_id"),
            user_id=item.get("user_id"),
            last_polled_at=(int(item["last_polled_at"]) if "last_polled_at" in item else None),
            poll_count=int(item.get("poll_count", 0)),
        )

    @classmethod
    def _grant_to_item(cls, grant: DeviceGrant) -> dict:
        item = {
            **cls._grant_key(grant.device_code_hash),
            "device_code_hash": grant.device_code_hash,
            "user_code": normalise_user_code(grant.user_code),
            "status": str(grant.status),
            "created_at": grant.created_at,
            "expires_at": grant.expires_at,
            "poll_count": grant.poll_count,
            # Mirrors expires_at: the row is worthless the moment it expires,
            # so let DynamoDB reap it rather than accumulating dead grants.
            "ttl": grant.expires_at,
        }
        if grant.session_id is not None:
            item["session_id"] = grant.session_id
        if grant.user_id is not None:
            item["user_id"] = grant.user_id
        if grant.last_polled_at is not None:
            item["last_polled_at"] = grant.last_polled_at
        return item

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(self, grant: DeviceGrant) -> None:
        """Write a pending grant and its user-code pointer atomically.

        Both writes are conditional on the key being free. A device-code
        collision is not a real scenario (256 bits of entropy), but a *user
        code* collision absolutely is — the alphabet is 22 characters over 8
        positions, and two live grants colliding would otherwise let one
        browser approval silently retarget the other CLI's grant. Failing the
        transaction lets the caller generate a fresh code and retry.

        Raises ``ClientError`` with code ``TransactionCanceledException`` when
        either key is taken.
        """
        if not self._enabled:
            return

        grant_item = self._grant_to_item(grant)
        pointer_item = {
            **self._pointer_key(grant.user_code),
            "device_code_hash": grant.device_code_hash,
            "ttl": grant.expires_at,
        }

        def _call() -> None:
            self._dynamodb.meta.client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": grant_item,
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": pointer_item,
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                ]
            )

        await asyncio.to_thread(_call)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get_by_device_code_hash(self, device_code_hash: str) -> Optional[DeviceGrant]:
        """Fetch a grant for a polling client. Expired rows are returned."""
        if not self._enabled:
            return None

        key = self._grant_key(device_code_hash)

        def _call() -> dict:
            return self._table.get_item(Key=key)

        try:
            response = await asyncio.to_thread(_call)
        except ClientError as exc:
            logger.error("Device grant get_item failed: %s", exc)
            return None

        item = response.get("Item")
        return self._item_to_grant(item) if item else None

    async def get_by_user_code(self, user_code: str) -> Optional[DeviceGrant]:
        """Resolve a human-typed user code to its grant. Two point reads."""
        if not self._enabled:
            return None

        key = self._pointer_key(user_code)

        def _call() -> dict:
            return self._table.get_item(Key=key)

        try:
            response = await asyncio.to_thread(_call)
        except ClientError as exc:
            logger.error("Device grant pointer get_item failed: %s", exc)
            return None

        pointer = response.get("Item")
        if not pointer:
            return None
        return await self.get_by_device_code_hash(pointer["device_code_hash"])

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    async def approve(
        self,
        device_code_hash: str,
        *,
        session_id: str,
        user_id: str,
        now: Optional[int] = None,
    ) -> bool:
        """Attach a session to a pending grant. Returns False if not pending.

        Conditional on ``status = pending`` *and* an unexpired ``expires_at``,
        so an approval racing the deadline cannot revive a dead grant. The
        expiry check is in the condition expression rather than read-then-write
        because the read and the write are not otherwise atomic.
        """
        if not self._enabled:
            return False

        stamp = now if now is not None else int(time.time())

        def _call() -> bool:
            try:
                self._table.update_item(
                    Key=self._grant_key(device_code_hash),
                    UpdateExpression=("SET #s = :approved, session_id = :sid, " "user_id = :uid, approved_at = :now"),
                    ConditionExpression=("attribute_exists(PK) AND #s = :pending " "AND expires_at > :now"),
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":approved": str(GrantStatus.APPROVED),
                        ":pending": str(GrantStatus.PENDING),
                        ":sid": session_id,
                        ":uid": user_id,
                        ":now": stamp,
                    },
                )
                return True
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    return False
                raise

        return await asyncio.to_thread(_call)

    async def claim(self, device_code_hash: str, *, now: Optional[int] = None) -> Optional[str]:
        """Hand over the session id exactly once. Returns None if unclaimable.

        This is the single-use gate. Two polls arriving together must not both
        receive the session value, so the transition to ``claimed`` is a
        conditional update on ``status = approved`` and the ``session_id``
        comes from ``ReturnValues=ALL_OLD`` — the pre-image of the *winning*
        write. A read-then-write would let both callers read ``approved`` and
        both return the value.

        Also conditional on the grant being unexpired, so a value cannot be
        collected after the deadline.
        """
        if not self._enabled:
            return None

        stamp = now if now is not None else int(time.time())

        def _call() -> Optional[str]:
            try:
                response = self._table.update_item(
                    Key=self._grant_key(device_code_hash),
                    UpdateExpression="SET #s = :claimed, claimed_at = :now",
                    ConditionExpression=("attribute_exists(PK) AND #s = :approved " "AND attribute_exists(session_id) " "AND expires_at > :now"),
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":claimed": str(GrantStatus.CLAIMED),
                        ":approved": str(GrantStatus.APPROVED),
                        ":now": stamp,
                    },
                    ReturnValues="ALL_OLD",
                )
                return response.get("Attributes", {}).get("session_id")
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    return None
                raise

        return await asyncio.to_thread(_call)

    async def deny(self, device_code_hash: str, *, now: Optional[int] = None) -> bool:
        """Record an explicit refusal. Returns False if no longer pending.

        Kept distinct from expiry so the CLI can say "you declined" instead of
        "it timed out".
        """
        if not self._enabled:
            return False

        stamp = now if now is not None else int(time.time())

        def _call() -> bool:
            try:
                self._table.update_item(
                    Key=self._grant_key(device_code_hash),
                    UpdateExpression="SET #s = :denied, denied_at = :now",
                    ConditionExpression="attribute_exists(PK) AND #s = :pending",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":denied": str(GrantStatus.DENIED),
                        ":pending": str(GrantStatus.PENDING),
                        ":now": stamp,
                    },
                )
                return True
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    return False
                raise

        return await asyncio.to_thread(_call)

    async def record_poll(self, device_code_hash: str, *, now: Optional[int] = None) -> None:
        """Stamp the poll for ``slow_down`` enforcement and abuse visibility.

        ``ADD`` on ``poll_count`` rather than a read-modify-write so
        concurrent polls cannot lose counts — the count is what makes an
        abusive client visible in logs.

        Best-effort: a failure here must not fail the poll itself, since the
        worst case is one unthrottled request.
        """
        if not self._enabled:
            return

        stamp = now if now is not None else int(time.time())

        def _call() -> None:
            self._table.update_item(
                Key=self._grant_key(device_code_hash),
                UpdateExpression=("SET last_polled_at = :now ADD poll_count :one"),
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeValues={":now": stamp, ":one": 1},
            )

        try:
            await asyncio.to_thread(_call)
        except ClientError as exc:
            logger.warning(
                "Device grant poll stamp failed for %s...: %s",
                device_code_hash[:8],
                exc,
            )

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete(self, grant: DeviceGrant) -> None:
        """Remove a grant and its pointer.

        TTL would collect both eventually; this exists so a completed or
        refused flow stops being readable immediately.
        """
        if not self._enabled:
            return

        grant_key = self._grant_key(grant.device_code_hash)
        pointer_key = self._pointer_key(grant.user_code)

        def _call() -> None:
            self._table.delete_item(Key=grant_key)
            self._table.delete_item(Key=pointer_key)

        await asyncio.to_thread(_call)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_repo: Optional[DeviceGrantRepository] = None


def get_device_grant_repository() -> DeviceGrantRepository:
    global _repo
    if _repo is None:
        _repo = DeviceGrantRepository()
    return _repo
