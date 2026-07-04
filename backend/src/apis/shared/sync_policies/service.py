"""Sync policy repository — DynamoDB storage for scheduled KB re-sync policies.

Lives in apis.shared because it has three independent consumers: app-api
(CRUD routes), the sync dispatcher Lambda (due sweep), and the sync worker
Lambda (run bookkeeping).

Storage (assistants table, adjacency list):
    PK: AST#{assistant_id} | SK: SYNCPOL#{policy_id}
    GSI4 (DueSyncIndex, sparse): GSI4_PK = "SYNCDUE", GSI4_SK = "{next_sync_at}#{policy_id}"
    GSI4 keys exist only while state == "active".
"""

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from .models import DUE_INDEX_PK, SyncInterval, SyncPolicy, SyncPolicyState, SyncRunResult, SyncSourceType

logger = logging.getLogger(__name__)

INTERVAL_DELTAS = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=30),
}

DEFAULT_MAX_POLICIES_PER_ASSISTANT = 10


class SyncPolicyLimitExceeded(Exception):
    """Raised when an assistant already has the maximum number of sync policies."""


class DuplicateSyncPolicy(Exception):
    """Raised when the source already has a sync policy on this assistant."""


def _get_current_timestamp() -> str:
    """Get current timestamp in ISO 8601 format"""
    return datetime.now(timezone.utc).isoformat() + "Z"


def _generate_policy_id() -> str:
    return f"syn-{uuid.uuid4().hex[:12]}"


def _table_name() -> str:
    table = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not table:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")
    return table


def _get_table():
    import boto3

    return boto3.resource("dynamodb").Table(_table_name())


def _due_sort_key(next_sync_at: str, policy_id: str) -> str:
    return f"{next_sync_at}#{policy_id}"


def max_policies_per_assistant() -> int:
    return int(os.environ.get("KB_SYNC_MAX_POLICIES_PER_ASSISTANT", DEFAULT_MAX_POLICIES_PER_ASSISTANT))


def compute_next_sync_at(interval: SyncInterval, from_time: Optional[datetime] = None) -> str:
    """Compute the next due timestamp for an interval, ISO 8601."""
    base = from_time or datetime.now(timezone.utc)
    return (base + INTERVAL_DELTAS[interval]).isoformat() + "Z"


async def create_sync_policy(
    assistant_id: str,
    source_type: SyncSourceType,
    source_ref: str,
    interval: SyncInterval,
    created_by_user_id: str,
) -> SyncPolicy:
    """Create an active sync policy due one interval from now.

    Enforces the per-assistant policy cap and one-policy-per-source
    uniqueness (both checked against the current policy list — bounded by
    the cap, so the scan is cheap).
    """
    existing = await list_sync_policies(assistant_id)
    if len(existing) >= max_policies_per_assistant():
        raise SyncPolicyLimitExceeded(
            f"Assistant {assistant_id} already has {len(existing)} sync policies (max {max_policies_per_assistant()})"
        )
    if any(p.source_ref == source_ref for p in existing):
        raise DuplicateSyncPolicy(f"Source {source_ref} already has a sync policy on assistant {assistant_id}")

    now = _get_current_timestamp()
    policy = SyncPolicy(
        policy_id=_generate_policy_id(),
        assistant_id=assistant_id,
        source_type=source_type,
        source_ref=source_ref,
        interval=interval,
        state="active",
        next_sync_at=compute_next_sync_at(interval),
        created_by_user_id=created_by_user_id,
        created_at=now,
        updated_at=now,
    )

    item = policy.model_dump(by_alias=True, exclude_none=True)
    item["PK"] = f"AST#{assistant_id}"
    item["SK"] = f"SYNCPOL#{policy.policy_id}"
    item["GSI4_PK"] = DUE_INDEX_PK
    item["GSI4_SK"] = _due_sort_key(policy.next_sync_at, policy.policy_id)

    _get_table().put_item(Item=item)
    logger.info(f"Created sync policy {policy.policy_id} ({source_type}/{interval}) for assistant {assistant_id}")
    return policy


async def get_sync_policy(assistant_id: str, policy_id: str) -> Optional[SyncPolicy]:
    response = _get_table().get_item(Key={"PK": f"AST#{assistant_id}", "SK": f"SYNCPOL#{policy_id}"})
    item = response.get("Item")
    return SyncPolicy.model_validate(item) if item else None


async def list_sync_policies(assistant_id: str) -> List[SyncPolicy]:
    from boto3.dynamodb.conditions import Key

    response = _get_table().query(
        KeyConditionExpression=Key("PK").eq(f"AST#{assistant_id}") & Key("SK").begins_with("SYNCPOL#")
    )
    policies = []
    for item in response.get("Items", []):
        try:
            policies.append(SyncPolicy.model_validate(item))
        except Exception as e:
            logger.warning(f"Failed to parse sync policy item: {e}")
    return policies


async def list_due_policies(now: Optional[str] = None, limit: int = 20) -> List[SyncPolicy]:
    """Query the sparse DueSyncIndex for active policies whose next_sync_at has passed.

    Returns policies most-overdue first. Paused policies have no GSI4 keys
    and are physically absent from this index.
    """
    from boto3.dynamodb.conditions import Key

    now = now or _get_current_timestamp()
    response = _get_table().query(
        IndexName="DueSyncIndex",
        # '~' sorts after '#' and all timestamp characters, so this covers
        # every "{ts}#{policy_id}" key with ts <= now.
        KeyConditionExpression=Key("GSI4_PK").eq(DUE_INDEX_PK) & Key("GSI4_SK").lte(f"{now}~"),
        Limit=limit,
        ScanIndexForward=True,
    )
    policies = []
    for item in response.get("Items", []):
        try:
            policies.append(SyncPolicy.model_validate(item))
        except Exception as e:
            logger.warning(f"Failed to parse due sync policy item: {e}")
    return policies


async def set_policy_state(
    assistant_id: str,
    policy_id: str,
    state: SyncPolicyState,
    next_sync_at: Optional[str] = None,
    state_reason: Optional[str] = None,
) -> bool:
    """Transition a policy's lifecycle state.

    Transition to "active" requires next_sync_at and (re)adds the GSI4 keys;
    any paused state REMOVEs them so the policy drops out of DueSyncIndex.
    Returns False if the policy does not exist.
    """
    from botocore.exceptions import ClientError

    if state == "active" and not next_sync_at:
        raise ValueError("next_sync_at is required when activating a sync policy")

    names = {"#state": "state"}
    values = {":state": state, ":updated_at": _get_current_timestamp()}
    set_parts = ["#state = :state", "updatedAt = :updated_at"]
    remove_parts = []

    if state == "active":
        set_parts += ["nextSyncAt = :next", "GSI4_PK = :gsi4pk", "GSI4_SK = :gsi4sk"]
        values[":next"] = next_sync_at
        values[":gsi4pk"] = DUE_INDEX_PK
        values[":gsi4sk"] = _due_sort_key(next_sync_at, policy_id)
        remove_parts.append("stateReason")
    else:
        remove_parts += ["GSI4_PK", "GSI4_SK"]
        if state_reason:
            set_parts.append("stateReason = :reason")
            values[":reason"] = state_reason

    expression = "SET " + ", ".join(set_parts)
    if remove_parts:
        expression += " REMOVE " + ", ".join(remove_parts)

    try:
        _get_table().update_item(
            Key={"PK": f"AST#{assistant_id}", "SK": f"SYNCPOL#{policy_id}"},
            UpdateExpression=expression,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(PK)",
        )
        logger.info(f"Sync policy {policy_id} -> {state}" + (f" ({state_reason})" if state_reason else ""))
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.warning(f"Cannot set state on missing sync policy {policy_id}")
            return False
        raise


async def rearm_policy(
    assistant_id: str,
    policy_id: str,
    expected_next_sync_at: str,
    new_next_sync_at: str,
    mark_run_started: bool = False,
) -> bool:
    """Advance next_sync_at, conditional on the currently stored value.

    The dispatcher re-arms BEFORE invoking the worker; the condition makes a
    double-fired tick idempotent — the second dispatcher loses the
    conditional write and skips the policy. Returns True if this caller won.

    mark_run_started stamps syncRunStartedAt in the same write, so winning
    the re-arm and claiming the in-flight slot are atomic.
    """
    from botocore.exceptions import ClientError

    update_expression = "SET nextSyncAt = :new, GSI4_SK = :gsi4sk, updatedAt = :updated_at"
    values = {
        ":new": new_next_sync_at,
        ":gsi4sk": _due_sort_key(new_next_sync_at, policy_id),
        ":updated_at": _get_current_timestamp(),
        ":expected": expected_next_sync_at,
    }
    if mark_run_started:
        update_expression += ", syncRunStartedAt = :run_started"
        values[":run_started"] = values[":updated_at"]

    try:
        _get_table().update_item(
            Key={"PK": f"AST#{assistant_id}", "SK": f"SYNCPOL#{policy_id}"},
            UpdateExpression=update_expression,
            ExpressionAttributeValues=values,
            ConditionExpression="nextSyncAt = :expected",
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.info(f"Sync policy {policy_id} already re-armed by another dispatcher; skipping")
            return False
        raise


async def record_sync_result(assistant_id: str, policy_id: str, result: SyncRunResult, not_found: bool = False) -> None:
    """Record a completed run's outcome and maintain the breaker counters.

    Success-ish results (changed/unchanged/skipped) reset both streaks;
    "failed" increments consecutiveFailures, plus consecutiveNotFound when
    the failure was a definitive source-gone (404). Clears the in-flight
    run stamp either way.
    """
    now = _get_current_timestamp()
    values = {":now": now, ":result": result}
    set_parts = ["lastSyncAt = :now", "lastResult = :result", "updatedAt = :now"]

    if result == "failed":
        values[":one"] = 1
        values[":zero"] = 0
        set_parts.append("consecutiveFailures = if_not_exists(consecutiveFailures, :zero) + :one")
        if not_found:
            set_parts.append("consecutiveNotFound = if_not_exists(consecutiveNotFound, :zero) + :one")
        else:
            set_parts.append("consecutiveNotFound = :zero")
    else:
        values[":zero"] = 0
        set_parts += ["consecutiveFailures = :zero", "consecutiveNotFound = :zero"]

    _get_table().update_item(
        Key={"PK": f"AST#{assistant_id}", "SK": f"SYNCPOL#{policy_id}"},
        UpdateExpression="SET " + ", ".join(set_parts) + " REMOVE syncRunStartedAt",
        ExpressionAttributeValues=values,
    )


# ── Reauth markers ───────────────────────────────────────────────────────────
#
# When the worker pauses a policy for re-consent it also writes a marker at
# PK=USER#{user_id}, SK=SYNCREAUTH#{policy_id} carrying {assistantId,
# providerId}. Consent completion queries this partition instead of scanning
# the table for the user's paused policies. Markers are advisory: resume
# re-verifies the policy still exists and is paused_reauth, so stale markers
# are harmless and are deleted on sight.


async def put_reauth_marker(user_id: str, assistant_id: str, policy_id: str, provider_id: str) -> None:
    _get_table().put_item(
        Item={
            "PK": f"USER#{user_id}",
            "SK": f"SYNCREAUTH#{policy_id}",
            "assistantId": assistant_id,
            "policyId": policy_id,
            "providerId": provider_id,
            "createdAt": _get_current_timestamp(),
        }
    )


async def delete_reauth_marker(user_id: str, policy_id: str) -> None:
    _get_table().delete_item(Key={"PK": f"USER#{user_id}", "SK": f"SYNCREAUTH#{policy_id}"})


async def resume_reauth_policies(user_id: str, provider_id: str) -> int:
    """Reactivate the user's paused_reauth policies for a provider.

    Called from the OAuth consent-completion path: a fresh grant is the ONLY
    thing that resumes a reauth pause. Resumed policies come due immediately.
    Returns the number resumed.
    """
    from boto3.dynamodb.conditions import Key

    response = _get_table().query(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with("SYNCREAUTH#")
    )
    resumed = 0
    now = _get_current_timestamp()
    for marker in response.get("Items", []):
        if marker.get("providerId") != provider_id:
            continue
        assistant_id = marker["assistantId"]
        policy_id = marker["policyId"]
        policy = await get_sync_policy(assistant_id, policy_id)
        if policy is not None and policy.state == "paused_reauth":
            await set_policy_state(assistant_id, policy_id, "active", next_sync_at=now)
            resumed += 1
        await delete_reauth_marker(user_id, policy_id)
    if resumed:
        logger.info(f"Resumed {resumed} paused_reauth sync policies for user {user_id} / provider {provider_id}")
    return resumed


async def resume_inactive_policies(assistant_id: str) -> int:
    """Reactivate an assistant's paused_inactive policies (due immediately).

    Called from the lastUsedAt bump path: the first use of a dormant
    assistant refreshes its knowledge base within a dispatcher tick.
    """
    resumed = 0
    now = _get_current_timestamp()
    for policy in await list_sync_policies(assistant_id):
        if policy.state == "paused_inactive":
            await set_policy_state(assistant_id, policy.policy_id, "active", next_sync_at=now)
            resumed += 1
    return resumed


async def change_policy_interval(assistant_id: str, policy_id: str, interval: SyncInterval) -> Optional[SyncPolicy]:
    """Change a policy's cadence. Active policies get re-armed one new
    interval out (and the due-index key moves with it); paused policies
    just remember the new interval for when they resume."""
    policy = await get_sync_policy(assistant_id, policy_id)
    if policy is None:
        return None

    values = {":interval": interval, ":updated_at": _get_current_timestamp()}
    set_parts = ["#interval = :interval", "updatedAt = :updated_at"]
    if policy.state == "active":
        next_sync_at = compute_next_sync_at(interval)
        set_parts += ["nextSyncAt = :next", "GSI4_SK = :gsi4sk"]
        values[":next"] = next_sync_at
        values[":gsi4sk"] = _due_sort_key(next_sync_at, policy_id)

    _get_table().update_item(
        Key={"PK": f"AST#{assistant_id}", "SK": f"SYNCPOL#{policy_id}"},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeNames={"#interval": "interval"},
        ExpressionAttributeValues=values,
    )
    return await get_sync_policy(assistant_id, policy_id)


RUN_NOW_COOLDOWN_SECONDS = 600


class RunNowCooldown(Exception):
    """Raised when a manual sync is requested within the cooldown window."""


async def trigger_run_now(assistant_id: str, policy_id: str) -> SyncPolicy:
    """Make a policy due immediately (manual "Sync now").

    Flows through the normal dispatcher path so every guard still applies.
    Only active policies can be manually run — paused states have their own
    explicit resume affordances. A conditional write on lastManualRunAt
    enforces the 10-minute cooldown atomically.
    """
    from botocore.exceptions import ClientError

    policy = await get_sync_policy(assistant_id, policy_id)
    if policy is None:
        raise KeyError(policy_id)
    if policy.state != "active":
        raise ValueError(f"Cannot run-now a policy in state {policy.state}")

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat() + "Z"
    cooldown_floor = (now_dt - timedelta(seconds=RUN_NOW_COOLDOWN_SECONDS)).isoformat() + "Z"
    try:
        _get_table().update_item(
            Key={"PK": f"AST#{assistant_id}", "SK": f"SYNCPOL#{policy_id}"},
            UpdateExpression="SET nextSyncAt = :now, GSI4_SK = :gsi4sk, lastManualRunAt = :now, updatedAt = :now",
            ExpressionAttributeValues={
                ":now": now,
                ":gsi4sk": _due_sort_key(now, policy_id),
                ":floor": cooldown_floor,
            },
            ConditionExpression="attribute_not_exists(lastManualRunAt) OR lastManualRunAt < :floor",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise RunNowCooldown(policy_id) from e
        raise
    return await get_sync_policy(assistant_id, policy_id)


async def delete_sync_policy(assistant_id: str, policy_id: str) -> bool:
    _get_table().delete_item(Key={"PK": f"AST#{assistant_id}", "SK": f"SYNCPOL#{policy_id}"})
    logger.info(f"Deleted sync policy {policy_id} for assistant {assistant_id}")
    return True


async def delete_sync_policies_for_source(assistant_id: str, source_ref: str) -> int:
    """Delete all policies referencing a source (document or crawl job).

    Called from the source's delete path so a removed document/crawl never
    leaves a live schedule behind.
    """
    policies = await list_sync_policies(assistant_id)
    deleted = 0
    for policy in policies:
        if policy.source_ref == source_ref:
            await delete_sync_policy(assistant_id, policy.policy_id)
            deleted += 1
    return deleted


async def delete_sync_policies_for_assistant(assistant_id: str) -> int:
    """Delete every sync policy under an assistant (assistant delete cascade)."""
    policies = await list_sync_policies(assistant_id)
    for policy in policies:
        await delete_sync_policy(assistant_id, policy.policy_id)
    if policies:
        logger.info(f"Deleted {len(policies)} sync policies for assistant {assistant_id}")
    return len(policies)
