"""DynamoDB repository for BFF session records.

Schema (single-table, fits the `BFFSessionsTable` provisioned in Phase 1):

    PK = SESSION#<session_id>
    SK = META

    Attrs: user_id, cognito_access_token, cognito_refresh_token, id_token,
           access_token_exp, csrf_secret, created_at, last_seen_at, ttl

    Cross-task refresh-lock attrs (added at runtime, never on the initial
    write — both default to "absent" until a refresh contender writes them):
       refresh_lock_owner: short opaque token identifying the leader
       refresh_lock_until: epoch seconds; lock is considered expired past this

The `ttl` attribute is wired to DynamoDB TTL so absolute session lifetime is
enforced by the table itself — even if a session row is somehow leaked from
the cleanup paths, DynamoDB will eventually evict it.

The refresh-lock attrs coordinate the Cognito refresh exchange across tasks:
the per-process `get_session_lock` and `single_flight` only coalesce within
a single Python process, so under `desiredCount > 1` two tasks can otherwise
issue parallel `cognito-idp:initiate_auth` calls with the same refresh token —
Cognito rotates on the first; the second fails `NotAuthorizedException` and
the loser unilaterally clears the user's cookie. The lock turns this into a
leader/follower handoff: one task does the refresh, the other reads the
freshly persisted tokens off the row.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from .models import SessionRecord

logger = logging.getLogger(__name__)


class SessionRepository:
    """Async-shaped wrapper over the BFF sessions DynamoDB table.

    The methods are ``async def`` and offload each boto3 call via
    ``asyncio.to_thread`` so the uvicorn event loop stays free to schedule
    unrelated coroutines during the DynamoDB round-trip. Without this
    offload, a single slow DDB call freezes every in-flight request — and
    under page-load fan-out the blocking calls serialize, producing the
    80s+ latency tails that motivated the event-loop-blocking bugfix.

    The ``_item_to_record`` translation and the post-read TTL
    defense-in-depth check run on the calling coroutine (pure Python, no
    I/O); only the boto3 round-trip is offloaded.
    """

    def __init__(self, table_name: Optional[str] = None) -> None:
        if table_name is None:
            table_name = os.environ.get("BFF_SESSIONS_TABLE_NAME", "")

        self._table_name = table_name
        self._enabled = bool(table_name)

        if self._enabled:
            self._dynamodb = boto3.resource("dynamodb")
            self._table = self._dynamodb.Table(table_name)
            logger.info("SessionRepository initialized with table: %s", table_name)
        else:
            self._dynamodb = None
            self._table = None
            logger.debug("SessionRepository disabled — BFF_SESSIONS_TABLE_NAME unset")

    @property
    def enabled(self) -> bool:
        return self._enabled

    @staticmethod
    def _key(session_id: str) -> dict:
        return {"PK": f"SESSION#{session_id}", "SK": "META"}

    @staticmethod
    def _item_to_record(item: dict) -> SessionRecord:
        return SessionRecord(
            session_id=item["session_id"],
            user_id=item["user_id"],
            username=item["username"],
            cognito_access_token=item["cognito_access_token"],
            cognito_refresh_token=item["cognito_refresh_token"],
            id_token=item.get("id_token"),
            access_token_exp=int(item["access_token_exp"]),
            csrf_secret=item["csrf_secret"],
            created_at=int(item["created_at"]),
            last_seen_at=int(item["last_seen_at"]),
            ttl=int(item["ttl"]),
        )

    @staticmethod
    def _record_to_item(record: SessionRecord) -> dict:
        item = {
            **SessionRepository._key(record.session_id),
            "session_id": record.session_id,
            "user_id": record.user_id,
            "username": record.username,
            "cognito_access_token": record.cognito_access_token,
            "cognito_refresh_token": record.cognito_refresh_token,
            "access_token_exp": record.access_token_exp,
            "csrf_secret": record.csrf_secret,
            "created_at": record.created_at,
            "last_seen_at": record.last_seen_at,
            "ttl": record.ttl,
        }
        if record.id_token is not None:
            item["id_token"] = record.id_token
        return item

    async def get(self, session_id: str) -> Optional[SessionRecord]:
        if not self._enabled:
            return None

        key = self._key(session_id)

        def _call() -> dict:
            return self._table.get_item(Key=key)

        try:
            response = await asyncio.to_thread(_call)
        except ClientError as exc:
            logger.error("BFF session get_item failed for %s: %s", session_id, exc)
            return None
        item = response.get("Item")
        if not item:
            return None
        # Defense in depth: if the row's TTL has passed but DDB hasn't swept
        # it yet (eventual TTL eviction is best-effort, not real-time),
        # treat it as missing.
        if int(item.get("ttl", 0)) <= int(time.time()):
            return None
        return self._item_to_record(item)

    async def put(self, record: SessionRecord) -> None:
        if not self._enabled:
            return

        item = self._record_to_item(record)

        def _call() -> None:
            self._table.put_item(Item=item)

        await asyncio.to_thread(_call)

    async def update_tokens(
        self,
        session_id: str,
        access_token: str,
        refresh_token: str,
        id_token: Optional[str],
        access_token_exp: int,
        last_seen_at: int,
        ttl: Optional[int] = None,
        expected_lock_owner: Optional[str] = None,
    ) -> None:
        """Atomically replace the Cognito tokens after a refresh.

        The refresh middleware calls this once it has a fresh access token.
        Note that Cognito's refresh-token rotation may issue a new refresh
        token too, hence the explicit `refresh_token` parameter. When `ttl`
        is supplied, the row's DynamoDB TTL slides forward in the same write
        — a refresh proves the user is active, so the session row's expiry
        should slide alongside it.

        When `expected_lock_owner` is supplied, the write is conditional on
        the row's `refresh_lock_owner` attribute strictly matching. The lock
        attributes are also REMOVED in the same write, releasing the
        cross-task lock alongside the token rotation. The condition fires
        on two distinct stale-leader cases that both must NOT stomp:

        1. A peer holds the lock right now (their owner != ours) — we never
           had it or our acquisition was stale.
        2. A peer held the lock, completed the refresh, and `REMOVE`d the
           attrs — the row has no lock owner at all but our tokens are
           now older than the row's persisted state.

        Both surface as `ConditionalCheckFailedException`; the caller
        re-reads the row and adopts the peer's tokens instead of stomping.
        """
        if not self._enabled:
            return
        update_expr = (
            "SET cognito_access_token = :at, "
            "cognito_refresh_token = :rt, "
            "access_token_exp = :exp, "
            "last_seen_at = :seen"
        )
        expr_values = {
            ":at": access_token,
            ":rt": refresh_token,
            ":exp": access_token_exp,
            ":seen": last_seen_at,
        }
        if id_token is not None:
            update_expr += ", id_token = :id"
            expr_values[":id"] = id_token
        if ttl is not None:
            update_expr += ", #ttl = :ttl"
            expr_values[":ttl"] = ttl
        kwargs: dict = {
            "Key": self._key(session_id),
            "UpdateExpression": update_expr,
            "ExpressionAttributeValues": expr_values,
        }
        if ttl is not None:
            # `ttl` is a reserved word in DynamoDB expressions.
            kwargs["ExpressionAttributeNames"] = {"#ttl": "ttl"}

        if expected_lock_owner is not None:
            # Atomically release the cross-task refresh lock alongside the
            # token write. The condition is strict — `refresh_lock_owner`
            # MUST equal our owner. We don't accept "lock attrs absent"
            # because that's exactly the stale-leader stomp case: a peer
            # whose lock TTL'd, took over, refreshed, and persisted (which
            # REMOVEs the lock attrs) — letting `attribute_not_exists`
            # match here would let our stale tokens overwrite the peer's
            # freshly rotated ones, silently logging the user out on the
            # next request when Cognito rejects our (now-revoked) refresh
            # token. The leader always set these attrs in
            # `try_acquire_refresh_lock`, so the strict form is correct
            # in every legitimate flow.
            kwargs["UpdateExpression"] = (
                update_expr + " REMOVE refresh_lock_owner, refresh_lock_until"
            )
            expr_values[":owner"] = expected_lock_owner
            kwargs["ConditionExpression"] = "refresh_lock_owner = :owner"

        def _call() -> None:
            self._table.update_item(**kwargs)

        await asyncio.to_thread(_call)

    async def try_acquire_refresh_lock(
        self,
        session_id: str,
        owner: str,
        lock_ttl_seconds: int,
    ) -> bool:
        """Atomically claim leadership of a cross-task Cognito refresh.

        Conditional `UpdateItem` on the session row: succeeds (returns True)
        only if no peer holds the lock OR the holder's lock has expired
        (`refresh_lock_until < now`). On contention returns False — the
        caller should poll the row for the leader's persisted tokens.

        Lock TTL bounds the worst case: a leader that crashes mid-refresh
        strands the lock for at most `lock_ttl_seconds` (we use 30s in the
        middleware), after which any peer can re-acquire and retry.

        Returns False on `ConditionalCheckFailedException`. Other DDB
        errors propagate so the caller can surface them as 5xx — silently
        suppressing them would create a "neither leader nor follower" gap.
        """
        if not self._enabled:
            return False
        now = int(time.time())
        kwargs: dict = {
            "Key": self._key(session_id),
            "UpdateExpression": (
                "SET refresh_lock_owner = :owner, "
                "refresh_lock_until = :until"
            ),
            # `attribute_exists(PK)` guards against UpdateItem's
            # upsert-by-default behavior — without it, a logout that races
            # the refresh path (deletes the row between `repository.get()`
            # and this call) would let us create a phantom row containing
            # only the lock attrs and no `ttl`, which DDB TTL would never
            # reap. With it, lock acquisition on a missing row fails
            # cleanly via ConditionalCheckFailedException → False.
            "ConditionExpression": (
                "attribute_exists(PK) AND ("
                "attribute_not_exists(refresh_lock_until) "
                "OR refresh_lock_until < :now)"
            ),
            "ExpressionAttributeValues": {
                ":owner": owner,
                ":until": now + lock_ttl_seconds,
                ":now": now,
            },
        }

        def _call() -> bool:
            try:
                self._table.update_item(**kwargs)
                return True
            except ClientError as exc:
                if (
                    exc.response.get("Error", {}).get("Code")
                    == "ConditionalCheckFailedException"
                ):
                    return False
                raise

        return await asyncio.to_thread(_call)

    async def release_refresh_lock(self, session_id: str, owner: str) -> None:
        """Release the cross-task refresh lock if `owner` still holds it.

        Used when the leader's Cognito refresh fails terminally and we want
        a peer to be able to retry without waiting for the full lock TTL.
        Best-effort: a `ConditionalCheckFailedException` (lock TTL'd or
        re-acquired) is treated as a no-op.

        `update_tokens` clears the lock attributes atomically with a
        successful refresh, so this is only for the failure path.
        """
        if not self._enabled:
            return
        kwargs: dict = {
            "Key": self._key(session_id),
            "UpdateExpression": (
                "REMOVE refresh_lock_owner, refresh_lock_until"
            ),
            "ConditionExpression": "refresh_lock_owner = :owner",
            "ExpressionAttributeValues": {":owner": owner},
        }

        def _call() -> None:
            try:
                self._table.update_item(**kwargs)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code == "ConditionalCheckFailedException":
                    return  # peer re-acquired or lock TTL'd — fine
                logger.warning(
                    "BFF refresh lock release failed for %s: %s",
                    session_id,
                    exc,
                )

        await asyncio.to_thread(_call)

    async def touch_last_seen(
        self,
        session_id: str,
        last_seen_at: int,
        ttl: Optional[int] = None,
    ) -> None:
        """Slide `last_seen_at` (and optionally `ttl`) without touching tokens.

        Used by the sliding-session path in `SessionRefreshMiddleware`: an
        active user that doesn't yet need a token refresh still needs the
        DDB TTL pushed forward so the row doesn't reap out from under them.
        """
        if not self._enabled:
            return
        update_expr = "SET last_seen_at = :seen"
        expr_values: dict = {":seen": last_seen_at}
        kwargs: dict = {
            "Key": self._key(session_id),
            "UpdateExpression": update_expr,
            "ExpressionAttributeValues": expr_values,
        }
        if ttl is not None:
            update_expr += ", #ttl = :ttl"
            expr_values[":ttl"] = ttl
            kwargs["UpdateExpression"] = update_expr
            kwargs["ExpressionAttributeNames"] = {"#ttl": "ttl"}

        def _call() -> None:
            self._table.update_item(**kwargs)

        try:
            await asyncio.to_thread(_call)
        except ClientError as exc:
            # Touch failures are non-critical — log and move on rather than
            # surfacing as a request error.
            logger.warning("BFF session touch failed for %s: %s", session_id, exc)

    async def delete(self, session_id: str) -> None:
        if not self._enabled:
            return

        key = self._key(session_id)

        def _call() -> None:
            self._table.delete_item(Key=key)

        await asyncio.to_thread(_call)
