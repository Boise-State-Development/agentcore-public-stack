"""Per-session single-flight lease (distributed concurrency guard).

Closes the server-side race where two concurrent ``POST /invocations`` for the
same session run two agent loops against one AgentCore Memory session and
corrupt tool-pairing history (see docs/specs/session-single-flight-guard.md and
PR #653). A client-side abort does not propagate through the AgentCore Runtime
data plane, and the Runtime can route the duplicate to a *different* container,
so an in-process lock is insufficient — we need a distributed lease.

Design:
- A dedicated item on the existing ``sessions-metadata`` table
  (``PK=USER#{user_id}``, ``SK=LEASE#{session_id}``). The deterministic key
  means acquisition is one atomic conditional write with no GSI read first.
- ``leaseExpiresAt`` (epoch seconds) is the application-level validity check
  used in the acquire ``ConditionExpression``; the ``ttl`` attribute is only a
  coarse DynamoDB auto-reap backstop for crashed-container orphans (TTL delete
  lags up to 48h and must never be the correctness mechanism).
- Renewal (heartbeat) and release are owner-scoped so a container that already
  lost the lease can neither extend nor delete the new owner's lease.

Fail-open: every operation except a genuine lock *conflict* proceeds/degrades
silently. A throttle or transient DynamoDB error must never block a legitimate
turn — the guard is a safety net, not a gate.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Lease validity window. A turn renews (heartbeats) well inside this; after a
# genuine container crash the lease self-expires within the window and the
# session becomes usable again. 90s tolerates one missed 30s heartbeat before
# expiry while keeping crash-recovery fast (well under the 600s stream timeout,
# so a normal long turn never self-evicts).
LEASE_WINDOW_SECONDS = 90

# Heartbeat cadence — how often the live turn renews ``leaseExpiresAt``.
LEASE_HEARTBEAT_SECONDS = 30

# DynamoDB ``ttl`` backstop: how long an orphaned lease item lingers before
# DynamoDB reaps it. Only prevents unbounded accumulation; correctness rides on
# ``leaseExpiresAt``.
LEASE_TTL_BACKSTOP_SECONDS = 3600


class SessionBusyError(Exception):
    """Raised on acquire when an unexpired lease is already held for the session.

    The caller (``/invocations``) maps this to HTTP 409.
    """


@dataclass(frozen=True)
class SessionLease:
    """Handle for a held lease — carries the owner token used by renew/release."""

    session_id: str
    user_id: str
    owner: str

    @property
    def pk(self) -> str:
        return f"USER#{self.user_id}"

    @property
    def sk(self) -> str:
        return f"LEASE#{self.session_id}"


def _table():
    """Return the sessions-metadata DynamoDB table, or ``None`` if unconfigured.

    Unconfigured (local dev without DynamoDB) is a valid state: the guard simply
    doesn't run there, matching the best-effort posture of the other
    session-metadata helpers.
    """
    table_name = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
    if not table_name:
        return None
    import boto3

    return boto3.resource("dynamodb").Table(table_name)


async def acquire_session_lease(
    session_id: str,
    user_id: str,
    *,
    force: bool = False,
) -> Optional[SessionLease]:
    """Acquire the single-flight lease for a session's turn.

    Args:
        session_id / user_id: the turn's session and its owning user.
        force: when True (resume / max-tokens continuation), take the lease
            unconditionally — those turns re-enter a loop that already ended and
            must never be blocked, but still install a lease so a fresh duplicate
            arriving *during* them is rejected.

    Returns:
        A ``SessionLease`` on success, or ``None`` when the guard is inactive
        (table unconfigured) or a non-conflict DynamoDB error occurred
        (fail-open — the turn proceeds unguarded rather than being blocked on
        lock-infra failure).

    Raises:
        SessionBusyError: a non-forced acquire found an unexpired lease held by
            another in-flight turn.
    """
    table = _table()
    if table is None:
        return None

    from botocore.exceptions import ClientError

    owner = uuid.uuid4().hex
    now = int(time.time())
    expires_at = now + LEASE_WINDOW_SECONDS
    ttl = now + LEASE_TTL_BACKSTOP_SECONDS

    update_kwargs = {
        "Key": {"PK": f"USER#{user_id}", "SK": f"LEASE#{session_id}"},
        "UpdateExpression": (
            "SET leaseOwner = :owner, leaseExpiresAt = :exp, "
            "#ttl = :ttl, updatedAt = :updated"
        ),
        "ExpressionAttributeNames": {"#ttl": "ttl"},
        "ExpressionAttributeValues": {
            ":owner": owner,
            ":exp": expires_at,
            ":ttl": ttl,
            ":updated": datetime.now(timezone.utc).isoformat(),
        },
    }
    if not force:
        # Win iff no lease exists or the existing one has lapsed. Two racing
        # duplicates both evaluate this against the same item; DynamoDB
        # serializes them so exactly one satisfies the condition.
        update_kwargs["ConditionExpression"] = (
            "attribute_not_exists(PK) OR leaseExpiresAt < :now"
        )
        update_kwargs["ExpressionAttributeValues"][":now"] = now

    try:
        table.update_item(**update_kwargs)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise SessionBusyError(session_id) from e
        # Any other DynamoDB failure: fail open. Log and let the turn run
        # unguarded — never block a legitimate turn on lock-infra trouble.
        logger.error(
            "Session lease acquire failed for %s (fail-open, proceeding unguarded): %s",
            session_id,
            e,
            exc_info=True,
        )
        return None

    logger.info(
        "Acquired session lease for %s (owner=%s, force=%s)",
        session_id,
        owner,
        force,
    )
    return SessionLease(session_id=session_id, user_id=user_id, owner=owner)


async def renew_session_lease(lease: Optional[SessionLease]) -> None:
    """Extend our lease's validity window. Best-effort, owner-scoped.

    Owner-conditional so a container that already lost the lease (its window
    lapsed and another turn took over) can't clobber the new owner's window.
    """
    if lease is None:
        return
    table = _table()
    if table is None:
        return

    from botocore.exceptions import ClientError

    now = int(time.time())
    try:
        table.update_item(
            Key={"PK": lease.pk, "SK": lease.sk},
            UpdateExpression="SET leaseExpiresAt = :exp, #ttl = :ttl, updatedAt = :updated",
            ConditionExpression="leaseOwner = :owner",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":exp": now + LEASE_WINDOW_SECONDS,
                ":ttl": now + LEASE_TTL_BACKSTOP_SECONDS,
                ":owner": lease.owner,
                ":updated": datetime.now(timezone.utc).isoformat(),
            },
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # We no longer own the lease (took too long, another turn took over).
            # Nothing to renew; the live loop will still finish, but the session
            # is no longer reserved for it.
            logger.warning("Session lease renew skipped for %s — no longer owner", lease.session_id)
            return
        logger.warning("Session lease renew failed for %s: %s", lease.session_id, e)


async def release_session_lease(lease: Optional[SessionLease]) -> None:
    """Release our lease at turn end. Best-effort, owner-scoped, idempotent.

    Owner-conditional delete so we never remove a lease a later turn legitimately
    took over after our window lapsed. Safe to call twice (the second is a no-op
    conditional miss) and safe to call with ``None``.
    """
    if lease is None:
        return
    table = _table()
    if table is None:
        return

    from botocore.exceptions import ClientError

    try:
        table.delete_item(
            Key={"PK": lease.pk, "SK": lease.sk},
            ConditionExpression="leaseOwner = :owner",
            ExpressionAttributeValues={":owner": lease.owner},
        )
        logger.info("Released session lease for %s", lease.session_id)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # Already released, expired-and-retaken, or never persisted — fine.
            return
        logger.warning("Session lease release failed for %s: %s", lease.session_id, e)
