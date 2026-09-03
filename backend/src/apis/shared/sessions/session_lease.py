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

# Heartbeat cadence — how often the live turn renews ``leaseExpiresAt`` and
# observes a cancel request. Also bounds worst-case Stop→resend latency: a
# cooperative stop is seen within one interval. Kept well under the window so a
# single missed tick never expires the lease.
LEASE_HEARTBEAT_SECONDS = 10

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
        # REMOVE clears any stale cancel marker when taking over an expired
        # lease item, so a prior turn's cancel request can't bleed into this
        # one. (Owner-scoping already protects — the new owner token won't match
        # the old cancelRequestedFor — but clearing keeps the row honest.)
        "UpdateExpression": (
            "SET leaseOwner = :owner, leaseExpiresAt = :exp, "
            "#ttl = :ttl, updatedAt = :updated "
            "REMOVE cancelRequestedFor, cancelRequestedAt"
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


async def renew_session_lease(lease: Optional[SessionLease]) -> bool:
    """Extend our lease's window and observe a cancel request in one round-trip.

    Owner-conditional so a container that already lost the lease (its window
    lapsed and another turn took over) can't clobber the new owner's window.
    Returns ``True`` iff a cancel has been requested for *this* lease owner —
    the caller flips the session manager's ``cancelled`` flag to stop the turn.
    Best-effort: any error (including having lost ownership) returns ``False``.
    """
    if lease is None:
        return False
    table = _table()
    if table is None:
        return False

    from botocore.exceptions import ClientError

    now = int(time.time())
    try:
        resp = table.update_item(
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
            ReturnValues="ALL_NEW",
        )
        attrs = resp.get("Attributes", {})
        # Owner-scoped: only honor a cancel aimed at *our* turn, so a stale
        # marker from a prior turn on the same row can't stop us.
        return attrs.get("cancelRequestedFor") == lease.owner
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # We no longer own the lease (took too long, another turn took over).
            # Nothing to renew; the live loop will still finish, but the session
            # is no longer reserved for it.
            logger.warning("Session lease renew skipped for %s — no longer owner", lease.session_id)
            return False
        logger.warning("Session lease renew failed for %s: %s", lease.session_id, e)
        return False


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


async def is_session_lease_held(session_id: str, user_id: str) -> bool:
    """Report whether an unexpired turn lease exists for this session.

    Read-only mirror of ``acquire_session_lease``'s condition, for a caller
    that has to *explain* a rejection it did not raise itself. The AgentCore
    Runtime data plane rewrites every non-2xx from the container into a
    ``424 Failed Dependency``, so app-api's chat proxy cannot tell the
    single-flight ``409`` apart from a container crash by status alone; this
    is the tiebreaker, asserting the same fact the container's 409 asserted.

    Best-effort in the safe direction: an unconfigured table or any DynamoDB
    error returns ``False``. A caller must never invent a conflict the guard
    itself would not have raised.
    """
    table = _table()
    if table is None:
        return False

    from botocore.exceptions import ClientError

    try:
        resp = table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": f"LEASE#{session_id}"}
        )
    except ClientError as e:
        logger.warning("Session lease lookup failed for %s: %s", session_id, e)
        return False

    item = resp.get("Item") or {}
    expires_at = item.get("leaseExpiresAt")
    if expires_at is None:
        return False
    try:
        return int(expires_at) > int(time.time())
    except (TypeError, ValueError):
        return False


async def request_session_cancel(session_id: str, user_id: str) -> bool:
    """Ask the turn currently holding this session's lease to stop.

    Called from the app-api ``user_stopped`` path (any container). Reads the
    lease's current ``leaseOwner`` and stamps ``cancelRequestedFor = <owner>``
    so the running container observes it on its next heartbeat and unwinds the
    turn. Owner-scoping is the safety property: the request names the *current*
    owner, so if the turn has already ended and a new one started (new owner
    token), the new turn ignores it — a stale Stop can never kill a later turn.

    Returns ``True`` if a cancel was armed against an active lease. ``False``
    when there is no active turn (no lease item) or the guard is inactive
    (table unconfigured) — both mean "nothing running to stop." Best-effort:
    any DynamoDB error returns ``False`` rather than raising.
    """
    table = _table()
    if table is None:
        return False

    from botocore.exceptions import ClientError

    try:
        resp = table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": f"LEASE#{session_id}"}
        )
    except ClientError as e:
        logger.warning("Session cancel lookup failed for %s: %s", session_id, e)
        return False

    item = resp.get("Item")
    owner = item.get("leaseOwner") if item else None
    if not owner:
        # No lease → no turn is streaming server-side for this session.
        return False

    try:
        table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": f"LEASE#{session_id}"},
            UpdateExpression="SET cancelRequestedFor = :owner, cancelRequestedAt = :ts",
            # Only arm if that same owner still holds the lease — otherwise the
            # turn already ended/rotated and there is nothing to cancel.
            ConditionExpression="leaseOwner = :owner",
            ExpressionAttributeValues={
                ":owner": owner,
                ":ts": datetime.now(timezone.utc).isoformat(),
            },
        )
        logger.info("Armed cancel for session %s (owner=%s)", session_id, owner)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # The turn ended or rotated between the read and the write — nothing
            # to cancel.
            return False
        logger.warning("Session cancel arm failed for %s: %s", session_id, e)
        return False


# ---------------------------------------------------------------------------
# Mid-turn steering inbox
# ---------------------------------------------------------------------------
#
# The same lease item doubles as a one-turn inbox for mid-turn steering
# (docs/specs/mid-turn-steering.md). A follow-up typed while a turn is
# streaming is appended to ``steerQueue`` and stamped with ``steerFor =
# <current leaseOwner>``; the container running the turn peeks the queue at
# each tool boundary and injects the text into the tool-result message.
#
# Riding the lease row rather than a new item buys three properties for free:
#
# * **Owner-scoping.** Exactly the ``cancelRequestedFor`` property — a steer
#   names the owner that was live when it was armed, so if the turn ended and
#   another started, the new turn ignores it.
# * **No GC.** ``release_session_lease`` deletes the whole row at turn end, so
#   an unconsumed inbox cannot outlive its turn.
# * **No new table, no new key derivation.** Same deterministic key.
#
# Consumption is commit-on-append, never commit-on-read: ``AfterToolsEvent``
# fires from a ``finally`` and so also fires on the interrupt path, where the
# mutated message is discarded. A hook that cleared on read would destroy the
# user's words whenever a steer landed on the same tool batch as an OAuth
# consent. So the hook peeks, and clears only once the message is confirmed in
# history.

# Cap on the inbox so a pathological client cannot grow the lease row toward
# the 400 KB item limit. Composer text is small and the row is deleted per
# turn, so these are guards, not budgets.
STEER_QUEUE_MAX_ENTRIES = 5
STEER_QUEUE_MAX_CHARS = 8000


class SteerQueueFullError(Exception):
    """Raised when a steer would exceed the inbox's entry or size cap.

    The caller (``POST /sessions/{id}/steer``) maps this to HTTP 429; the SPA
    leaves the entry queued for the end-of-turn flush.
    """


def _steer_entries(item: Optional[dict], owner: Optional[str] = None) -> list:
    """Return the inbox entries on a lease item, owner-scoped when asked.

    Returns ``[]`` for a missing item, a missing/foreign ``steerFor``, or a
    malformed queue — every read path treats "no entries" as the safe answer.
    """
    if not item:
        return []
    if owner is not None and item.get("steerFor") != owner:
        return []
    queue = item.get("steerQueue")
    if not isinstance(queue, list):
        return []
    return [e for e in queue if isinstance(e, dict) and e.get("id") and e.get("text")]


async def request_session_steer(
    session_id: str,
    user_id: str,
    *,
    text: str,
    entry_id: str,
) -> bool:
    """Queue a follow-up for injection into the turn holding this session's lease.

    Called from app-api (any container), mirroring ``request_session_cancel``:
    read the lease's current ``leaseOwner``, then conditionally append to the
    inbox naming that same owner. The condition is the whole safety property —
    if the turn ended between the read and the write, the append is rejected
    and the caller falls back to sending the text as a normal turn.

    Returns ``True`` if the entry was queued against an active lease. ``False``
    when there is no active turn (no lease item), the turn ended mid-flight, or
    the guard is inactive (table unconfigured) — all of which mean "nothing
    running to steer", and all of which the SPA handles the same way.

    Raises:
        SteerQueueFullError: the inbox is at its entry or character cap.
    """
    table = _table()
    if table is None:
        return False

    from botocore.exceptions import ClientError

    try:
        resp = table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": f"LEASE#{session_id}"}
        )
    except ClientError as e:
        logger.warning("Session steer lookup failed for %s: %s", session_id, e)
        return False

    item = resp.get("Item")
    owner = item.get("leaseOwner") if item else None
    if not owner:
        # No lease → no turn is streaming server-side for this session.
        return False

    existing = _steer_entries(item, owner)
    if len(existing) >= STEER_QUEUE_MAX_ENTRIES:
        raise SteerQueueFullError(session_id)
    if sum(len(str(e.get("text", ""))) for e in existing) + len(text) > STEER_QUEUE_MAX_CHARS:
        raise SteerQueueFullError(session_id)

    entry = {
        "id": entry_id,
        "text": text,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": f"LEASE#{session_id}"},
            UpdateExpression=(
                "SET steerFor = :owner, steerRequestedAt = :ts, "
                "steerQueue = list_append(if_not_exists(steerQueue, :empty), :entry)"
            ),
            # Only arm if that same owner still holds the lease — otherwise the
            # turn already ended/rotated and there is nothing to steer.
            ConditionExpression="leaseOwner = :owner",
            ExpressionAttributeValues={
                ":owner": owner,
                ":ts": entry["at"],
                ":empty": [],
                ":entry": [entry],
            },
        )
        logger.info("Queued steer for session %s (owner=%s)", session_id, owner)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # The turn ended or rotated between the read and the write. The
            # correct outcome, not an error: the caller sends a normal turn.
            return False
        logger.warning("Session steer arm failed for %s: %s", session_id, e)
        return False


async def peek_steer_queue(lease: Optional[SessionLease]) -> list:
    """Read the inbox entries armed for *our* lease, without consuming them.

    Called from the steering hook at each tool boundary. Deliberately a peek:
    the entry is cleared only once the message carrying it is confirmed in
    history (``clear_steer_entry``), so an injection discarded by the interrupt
    path is re-delivered rather than lost.

    Best-effort in the safe direction: an unconfigured table, a missing row, a
    foreign ``steerFor``, or any DynamoDB error all return ``[]``.
    """
    if lease is None:
        return []
    table = _table()
    if table is None:
        return []

    from botocore.exceptions import ClientError

    try:
        resp = table.get_item(Key={"PK": lease.pk, "SK": lease.sk})
    except ClientError as e:
        logger.warning("Steer queue read failed for %s: %s", lease.session_id, e)
        return []

    return _steer_entries(resp.get("Item"), lease.owner)


def _remove_entry_by_id(
    table,
    pk: str,
    sk: str,
    entry_id: str,
    *,
    owner: Optional[str] = None,
) -> bool:
    """Conditionally remove one inbox entry by id. Idempotent, best-effort.

    DynamoDB removes list elements by index, so we read to find the entry's
    position and then guard the write on that index still holding that id (and,
    when given, on the lease still being ours). A concurrent removal that
    shifted the list fails the condition rather than deleting the wrong entry —
    a re-delivery is recoverable, deleting someone's words is not.
    """
    from botocore.exceptions import ClientError

    try:
        resp = table.get_item(Key={"PK": pk, "SK": sk})
    except ClientError as e:
        logger.warning("Steer entry lookup failed: %s", e)
        return False

    item = resp.get("Item")
    if not item:
        return False
    if owner is not None and item.get("leaseOwner") != owner:
        return False

    queue = item.get("steerQueue")
    if not isinstance(queue, list):
        return False
    index = next(
        (
            i
            for i, e in enumerate(queue)
            if isinstance(e, dict) and e.get("id") == entry_id
        ),
        None,
    )
    if index is None:
        # Already cleared — the caller's intent is satisfied.
        return False

    values = {":id": entry_id}
    condition = f"steerQueue[{index}].id = :id"
    if owner is not None:
        condition += " AND leaseOwner = :owner"
        values[":owner"] = owner

    try:
        table.update_item(
            Key={"PK": pk, "SK": sk},
            UpdateExpression=f"REMOVE steerQueue[{index}]",
            ConditionExpression=condition,
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # The list shifted under us, or the lease rotated. Leaving the entry
            # in place is the safe failure: worst case it is re-injected once.
            return False
        logger.warning("Steer entry clear failed: %s", e)
        return False


async def clear_steer_entry(lease: Optional[SessionLease], entry_id: str) -> bool:
    """Consume one inbox entry, once its injection is committed to history.

    The second half of commit-on-append: called from the steering hook's
    ``MessageAddedEvent`` handler, which is the first point at which the
    injected text is really in the conversation. Owner-scoped and conditional
    on the entry id, so a re-delivery after a lost ack is idempotent rather
    than duplicated.
    """
    if lease is None:
        return False
    table = _table()
    if table is None:
        return False
    return _remove_entry_by_id(table, lease.pk, lease.sk, entry_id, owner=lease.owner)


async def remove_steer_entry(session_id: str, user_id: str, entry_id: str) -> bool:
    """Withdraw a queued steer on the user's behalf (composer entry removed).

    The client-facing counterpart of ``clear_steer_entry``: no lease owner in
    hand, and none needed — the user is deleting their own words from their own
    session's inbox, and the ``USER#`` partition already scopes that. Returns
    ``False`` for an unknown id or an ended turn; the caller answers 204 either
    way, since the user's intent is satisfied in both cases.
    """
    table = _table()
    if table is None:
        return False
    return _remove_entry_by_id(
        table, f"USER#{user_id}", f"LEASE#{session_id}", entry_id
    )
