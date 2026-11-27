"""Headless-run grants — the durable "act as me" record for unattended runs.

A headless run mints a per-owner Cognito access token (see
``apis.shared.harness.auth``). The credential that mint consumes must NOT be
a silently-reused BFF browser session: sessions are an authentication
artifact with an 8-hour sliding TTL and no user-visible lifecycle. Instead,
each user who enables headless runs gets an explicit **headless-grant
record** with its own consent/revocation lifecycle:

* **Create-on-enable** — when a user turns on the feature (today: the "Run
  now" route; later: the schedules SPA), the grant pins the refresh token
  from their *live, attended* session. Enabling again re-pins the token and
  slides the expiry window.
* **Lookup** — unattended callers resolve the newest active grant by
  ``user_id`` via a sparse GSI (a direct query, replacing the spike's
  full-table ``Scan``).
* **Revoke** — the user (or an admin) can kill the grant at any time; the
  stored refresh token is removed in the same write so a revoked record
  retains no usable credential.

Storage: items live in the BFF sessions table (same data classification —
it already holds Cognito refresh tokens — and the same IAM grants), keyed
``PK=HEADLESS-GRANT#{grant_id}, SK=META`` so they are invisible to every
session access path (sessions key by ``SESSION#{id}``). Only grant items
carry the ``grant_user_id`` attribute, so the ``HeadlessGrantUserIndex``
GSI (``grant_user_id`` / ``created_at``) is sparse: session rows never
project into it.

**Login-recency policy (documented product decision):** the platform may
act headlessly as a user only within ``HEADLESS_GRANT_MAX_AGE_DAYS``
(default **30**, matching the Cognito app client's refresh-token validity —
the CDK default we deploy with) of the login that produced the pinned
token. The grant's DynamoDB TTL is anchored to that login
(``token_issued_at``), so the record expires no later than the token it
wraps. Cognito remains the hard ceiling either way: a refresh exchange
against a token older than the pool's validity fails and surfaces as
``HeadlessAuthError``, which scheduled callers should treat as
"pause until the user logs in again" (the KB-sync ``paused_reauth``
analog).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

#: "Must have logged in within N days" — see the module docstring. Matches
#: the Cognito refresh-token validity (CDK default: 30 days).
HEADLESS_GRANT_MAX_AGE_DAYS = 30

GRANT_USER_INDEX_NAME = "HeadlessGrantUserIndex"

_STATUS_ACTIVE = "active"
_STATUS_REVOKED = "revoked"
#: Cognito refused the grant's refresh token (signed out of the session it was
#: pinned from before logout kept it alive, expired, or revoked upstream). The
#: grant can't mint until the user enables scheduled runs again, which re-pins
#: it and sets it back to active. Not "active", so ``GET /runs/grant`` reads
#: ``enabled: false`` and ``/schedules`` offers Enable again.
_STATUS_NEEDS_REAUTH = "needs_reauth"


def _max_age_seconds() -> int:
    days = int(os.environ.get("HEADLESS_GRANT_MAX_AGE_DAYS", HEADLESS_GRANT_MAX_AGE_DAYS))
    return days * 24 * 60 * 60


@dataclass
class HeadlessGrant:
    """One user's standing consent for the platform to run as them."""

    grant_id: str
    user_id: str
    username: str  # Cognito username; required for SECRET_HASH on refresh
    cognito_refresh_token: str
    status: str  # "active" | "needs_reauth" | "revoked"
    created_at: int  # epoch seconds (grant creation)
    updated_at: int  # epoch seconds (last enable/re-pin or token rotation)
    token_issued_at: int  # epoch seconds — login that produced the token
    ttl: int  # epoch seconds; DynamoDB TTL = token_issued_at + max age
    last_used_at: Optional[int] = None
    revoked_at: Optional[int] = None
    #: The BFF session whose refresh token the grant holds. Logout and a re-pin
    #: read it to decide whether anyone else still holds the token before it is
    #: revoked. ``None`` on a grant pinned before this was recorded.
    pinned_session_id: Optional[str] = None

    @property
    def is_active(self) -> bool:
        return self.status == _STATUS_ACTIVE and self.ttl > int(time.time())

    @property
    def is_renewable(self) -> bool:
        """Active, or waiting to be re-enabled, and inside its window: ``enable`` reuses it."""
        return self.status in (_STATUS_ACTIVE, _STATUS_NEEDS_REAUTH) and self.ttl > int(time.time())


class HeadlessGrantService:
    """Create-on-enable / lookup / revoke for headless-run grants.

    Async-shaped like ``SessionRepository``: every boto3 round-trip is
    offloaded via ``asyncio.to_thread`` so the event loop stays free.
    """

    def __init__(
        self,
        *,
        table_name: Optional[str] = None,
        region: Optional[str] = None,
    ) -> None:
        self._table_name = table_name or os.environ.get("BFF_SESSIONS_TABLE_NAME", "")
        self._region = region or os.environ.get("AWS_REGION", "us-west-2")
        self._table = None

    @property
    def enabled(self) -> bool:
        return bool(self._table_name)

    def _get_table(self):
        if self._table is None:
            if not self._table_name:
                raise RuntimeError(
                    "BFF_SESSIONS_TABLE_NAME is required for headless grants"
                )
            self._table = boto3.resource(
                "dynamodb", region_name=self._region
            ).Table(self._table_name)
        return self._table

    @staticmethod
    def _key(grant_id: str) -> dict:
        return {"PK": f"HEADLESS-GRANT#{grant_id}", "SK": "META"}

    @staticmethod
    def _item_to_grant(item: dict) -> HeadlessGrant:
        return HeadlessGrant(
            grant_id=item["grant_id"],
            user_id=item["grant_user_id"],
            username=item["username"],
            cognito_refresh_token=item.get("cognito_refresh_token", ""),
            status=item["status"],
            created_at=int(item["created_at"]),
            updated_at=int(item["updated_at"]),
            token_issued_at=int(item["token_issued_at"]),
            ttl=int(item["ttl"]),
            last_used_at=int(item["last_used_at"]) if "last_used_at" in item else None,
            revoked_at=int(item["revoked_at"]) if "revoked_at" in item else None,
            pinned_session_id=item.get("pinned_session_id") or None,
        )

    def _query_grants_sync(self, user_id: str) -> list[dict]:
        """Newest-first grant items for a user via the sparse GSI."""
        table = self._get_table()
        items: list[dict] = []
        kwargs: dict = {
            "IndexName": GRANT_USER_INDEX_NAME,
            "KeyConditionExpression": Key("grant_user_id").eq(user_id),
            "ScanIndexForward": False,
        }
        while True:
            page = table.query(**kwargs)
            items.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return items

    async def get_active_grant(self, user_id: str) -> Optional[HeadlessGrant]:
        """Return the newest active, unexpired grant for ``user_id``.

        Expiry is checked application-side too (DynamoDB TTL eviction is
        best-effort, not real-time — same defense-in-depth as the session
        repository).
        """
        items = await asyncio.to_thread(self._query_grants_sync, user_id)
        for item in items:
            grant = self._item_to_grant(item)
            if grant.is_active:
                return grant
        return None

    async def _get_renewable_grant(self, user_id: str) -> Optional[HeadlessGrant]:
        """The newest grant ``enable`` may re-pin: active or ``needs_reauth``, unexpired. Never a revoked one."""
        items = await asyncio.to_thread(self._query_grants_sync, user_id)
        for item in items:
            grant = self._item_to_grant(item)
            if grant.is_renewable:
                return grant
        return None

    async def enable(
        self,
        *,
        user_id: str,
        username: str,
        refresh_token: str,
        token_issued_at: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> HeadlessGrant:
        """Create (or renew) the user's grant from an attended session.

        Callers pass the refresh token from the user's *live* BFF session
        plus that session's ``created_at`` as ``token_issued_at`` — the
        login instant anchors the grant's TTL, because Cognito's
        refresh-token validity runs from token issuance, not from when we
        pin it. Re-enabling re-pins the token onto the existing grant
        (stable ``grant_id`` for audit continuity) and slides the window. A
        grant marked ``needs_reauth`` is re-pinned the same way and becomes
        active again; a revoked one is never reused, so enabling after a
        revoke creates a new grant. ``session_id`` is the session the token
        came from, recorded as ``pinned_session_id``.
        """
        now = int(time.time())
        issued_at = token_issued_at or now
        ttl = issued_at + _max_age_seconds()

        existing = await self._get_renewable_grant(user_id)
        if existing is not None:
            def _renew() -> None:
                values = {
                    ":rt": refresh_token,
                    ":now": now,
                    ":iss": issued_at,
                    ":ttl": ttl,
                    ":active": _STATUS_ACTIVE,
                }
                set_parts = (
                    "SET cognito_refresh_token = :rt, updated_at = :now, "
                    "token_issued_at = :iss, #ttl = :ttl, #s = :active"
                )
                if session_id:
                    set_parts += ", pinned_session_id = :sid"
                    values[":sid"] = session_id
                self._get_table().update_item(
                    Key=self._key(existing.grant_id),
                    UpdateExpression=set_parts + (" REMOVE needs_reauth_at" if session_id else " REMOVE needs_reauth_at, pinned_session_id"),
                    ExpressionAttributeNames={"#ttl": "ttl", "#s": "status"},
                    ExpressionAttributeValues=values,
                )

            await asyncio.to_thread(_renew)
            logger.info(
                "Renewed headless grant %s for user %s", existing.grant_id, user_id
            )
            existing.cognito_refresh_token = refresh_token
            existing.updated_at = now
            existing.token_issued_at = issued_at
            existing.ttl = ttl
            existing.status = _STATUS_ACTIVE
            existing.pinned_session_id = session_id
            return existing

        grant = HeadlessGrant(
            grant_id=f"hlg-{uuid.uuid4().hex[:12]}",
            user_id=user_id,
            username=username,
            cognito_refresh_token=refresh_token,
            status=_STATUS_ACTIVE,
            created_at=now,
            updated_at=now,
            token_issued_at=issued_at,
            ttl=ttl,
            pinned_session_id=session_id,
        )

        def _put() -> None:
            self._get_table().put_item(
                Item={
                    **self._key(grant.grant_id),
                    "grant_id": grant.grant_id,
                    "grant_user_id": grant.user_id,
                    "username": grant.username,
                    "cognito_refresh_token": grant.cognito_refresh_token,
                    "status": grant.status,
                    "created_at": grant.created_at,
                    "updated_at": grant.updated_at,
                    "token_issued_at": grant.token_issued_at,
                    "ttl": grant.ttl,
                    **({"pinned_session_id": session_id} if session_id else {}),
                }
            )

        await asyncio.to_thread(_put)
        logger.info("Created headless grant %s for user %s", grant.grant_id, user_id)
        return grant

    async def revoke(self, user_id: str) -> bool:
        """Revoke every active grant for ``user_id``.

        The stored refresh token is REMOVEd in the same write — a revoked
        record keeps its audit fields but no usable credential. Returns
        True iff at least one grant was revoked.
        """
        items = await asyncio.to_thread(self._query_grants_sync, user_id)
        now = int(time.time())
        revoked_any = False
        for item in items:
            if item.get("status") not in (_STATUS_ACTIVE, _STATUS_NEEDS_REAUTH):
                continue
            grant_id = str(item["grant_id"])

            def _revoke(gid: str = grant_id) -> None:
                self._get_table().update_item(
                    Key=self._key(gid),
                    UpdateExpression=(
                        "SET #s = :revoked, revoked_at = :now, updated_at = :now "
                        "REMOVE cognito_refresh_token"
                    ),
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":revoked": _STATUS_REVOKED, ":now": now},
                )

            await asyncio.to_thread(_revoke)
            logger.info("Revoked headless grant %s for user %s", grant_id, user_id)
            revoked_any = True
        return revoked_any

    async def mark_needs_reauth(self, grant_id: str, refresh_token: str) -> bool:
        """Mark a grant whose refresh token Cognito refused. Returns whether it changed it.

        Conditional on the grant still being active and still holding
        ``refresh_token``, so a re-pin that landed between the refused mint and
        this write is never undone. The dead token is REMOVEd in the same
        write: a grant keeps no credential it can't use.
        """

        def _mark() -> bool:
            now = int(time.time())
            try:
                self._get_table().update_item(
                    Key=self._key(grant_id),
                    UpdateExpression=(
                        "SET #s = :needs, needs_reauth_at = :now, updated_at = :now "
                        "REMOVE cognito_refresh_token"
                    ),
                    ConditionExpression="#s = :active AND cognito_refresh_token = :rt",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":needs": _STATUS_NEEDS_REAUTH,
                        ":active": _STATUS_ACTIVE,
                        ":rt": refresh_token,
                        ":now": now,
                    },
                )
                return True
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    return False
                raise

        marked = await asyncio.to_thread(_mark)
        if marked:
            logger.info("Headless grant %s marked needs_reauth: Cognito refused its token", grant_id)
        return marked

    async def persist_rotated_refresh_token(
        self, grant_id: str, refresh_token: str
    ) -> None:
        """Persist a rotated refresh token back onto the grant.

        The pool we deploy does not rotate refresh tokens today, but if
        rotation is ever enabled the old token dies the moment Cognito
        rotates it — failing to persist the replacement would strand the
        grant after one headless mint. Callers treat failures as fatal for
        the mint (better to fail loudly than to silently burn the grant's
        last valid token).
        """

        def _update() -> None:
            self._get_table().update_item(
                Key=self._key(grant_id),
                UpdateExpression=(
                    "SET cognito_refresh_token = :rt, updated_at = :now"
                ),
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeValues={
                    ":rt": refresh_token,
                    ":now": int(time.time()),
                },
            )

        await asyncio.to_thread(_update)

    async def record_use(self, grant_id: str) -> None:
        """Best-effort ``last_used_at`` touch — never fails a run."""

        def _touch() -> None:
            self._get_table().update_item(
                Key=self._key(grant_id),
                UpdateExpression="SET last_used_at = :now",
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeValues={":now": int(time.time())},
            )

        try:
            await asyncio.to_thread(_touch)
        except (ClientError, RuntimeError) as exc:
            logger.warning("Headless grant %s last-used touch failed: %s", grant_id, exc)
