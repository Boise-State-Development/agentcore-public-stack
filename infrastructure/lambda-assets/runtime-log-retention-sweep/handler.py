"""Daily retention sweep over this deployment's AgentCore Runtime log groups.

WHY THIS EXISTS
---------------
The AgentCore service, not CloudFormation, creates a Runtime's log group
(``/aws/bedrock-agentcore/runtimes/<runtime-name>-<runtime-id>-<endpoint>``).
The stack's ``RuntimeLogRetention`` custom resource sets retention on the
live ``-DEFAULT`` group once, at deploy time. That leaves three gaps:

- A replaced Runtime gets a new id and a new group. The old group is outside
  CDK from then on and keeps whatever retention it had. Groups orphaned before
  that custom resource existed have none at all, so they never expire.
- Account governance can overwrite the value afterwards. A landing zone that
  stamps a default retention on every ``CreateLogGroup`` event does so minutes
  after the deploy-time call, so the deploy-time value does not survive even
  on the live group.
- Groups for endpoints other than ``DEFAULT`` are never touched.

These groups can hold conversation text, so "never expires" is a privacy
problem as well as a storage cost.

WHAT IT DOES
------------
Lists every log group whose name starts with ``LOG_GROUP_PREFIX`` and sets
``RETENTION_IN_DAYS`` on any whose retention is unset or longer. A shorter
retention is left alone: tightening is always somebody's deliberate choice.

The prefix is ``/aws/bedrock-agentcore/runtimes/<runtime-name>-``. AgentCore
Runtime names cannot contain ``-``, so that prefix matches every generation of
this deployment's Runtime and nothing that belongs to another deployment in the
same account.

It never deletes a group, and never reads log events.

Invoke with ``{"dryRun": true}`` to report what would change without changing it.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_RUNTIMES_ROOT = "/aws/bedrock-agentcore/runtimes/"

_client = None


def _logs_client() -> Any:
    global _client
    if _client is None:
        _client = boto3.client("logs")
    return _client


def needs_retention(current: Optional[int], target: int) -> bool:
    """True when the group keeps data longer than ``target`` days."""
    return current is None or current > target


def sweep(client: Any, prefix: str, target: int, dry_run: bool = False) -> Dict[str, Any]:
    matched = 0
    updated: List[str] = []
    failed: List[str] = []

    paginator = client.get_paginator("describe_log_groups")
    for page in paginator.paginate(logGroupNamePrefix=prefix):
        for group in page.get("logGroups", []):
            matched += 1
            name = group["logGroupName"]
            if not needs_retention(group.get("retentionInDays"), target):
                continue
            if dry_run:
                updated.append(name)
                continue
            try:
                client.put_retention_policy(logGroupName=name, retentionInDays=target)
                updated.append(name)
            except Exception:  # noqa: BLE001 - one bad group must not stop the sweep
                logger.exception("PutRetentionPolicy failed for %s", name)
                failed.append(name)

    return {
        "prefix": prefix,
        "retentionInDays": target,
        "dryRun": dry_run,
        "matched": matched,
        "updated": updated,
        "failed": failed,
    }


def handler(event: Optional[Dict[str, Any]], context: Any) -> Dict[str, Any]:
    prefix = os.environ["LOG_GROUP_PREFIX"]
    target = int(os.environ["RETENTION_IN_DAYS"])
    # A prefix that is too short would reach other deployments' groups.
    runtime_name = prefix.removeprefix(_RUNTIMES_ROOT).removesuffix("-")
    if not prefix.startswith(_RUNTIMES_ROOT) or not prefix.endswith("-") or not runtime_name:
        raise ValueError(f"Refusing to sweep unscoped prefix {prefix!r}")

    dry_run = bool((event or {}).get("dryRun", False))
    result = sweep(_logs_client(), prefix, target, dry_run=dry_run)
    logger.info(
        "Retention sweep: matched=%d updated=%d failed=%d dryRun=%s",
        result["matched"],
        len(result["updated"]),
        len(result["failed"]),
        dry_run,
    )
    if result["failed"]:
        # Surface through the Lambda Errors metric (and its alarm) rather
        # than only in a log line nobody reads.
        raise RuntimeError(f"PutRetentionPolicy failed for {len(result['failed'])} log group(s)")
    return result
