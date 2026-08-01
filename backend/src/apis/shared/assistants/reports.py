"""Problem reports for the Agent Marketplace (D15, Phase 8).

A report is a **private message to the curator**. It is never rendered to another browsing
user, and nothing derived from it may reach ``usageCount``, the store front, or any
ordering — the moment report volume influences placement, reporting becomes a way to bury
a competitor's Agent. This module therefore has exactly two readers: the admin queue and
the delete path that removes reports along with their Agent.

Storage is child rows under the Agent on the **assistants** table, so a report is deleted
with the Agent it concerns and never outlives it:

    PK = AST#{agent_id}, SK = REPORT#{report_id}
    { reporterId, reporterName, reason, note, state, createdAt,
      resolvedAt?, resolvedBy?, resolutionNote? }

Written only while ``state == "open"``, exactly as GSI5 is written only while published:

    GSI6_PK = "REPORTS#OPEN"   GSI6_SK = CREATED#{created_at}

Four things here are load-bearing:

**1. ⚠️ The sort key is deterministic, not chronological.** The spec sketched
``SK = REPORT#{created_at}#{report_id}``, but that is incompatible with its own D15.4
instruction to enforce one-open-report-per-reporter "with a conditional write on a
deterministic ``report_id``, not a second index": you cannot conditionally update a row
whose key embeds the timestamp you are trying *not* to change without first reading it —
which is the extra lookup D15.4 exists to avoid. So the key is
``REPORT#{sha256(agent_id:reporter_id)}`` and the chronology lives in ``GSI6_SK``, which
is the only place anything actually reads it. Nothing sorts by the table's sort key.

**2. The one-open-report rule is enforced by three conditional writes and no read.**
Create-if-absent, else update-if-open (preserving ``createdAt`` and the index keys, so a
re-report does not jump the queue), else overwrite-if-closed with a fresh ``createdAt``.
Each step is a condition on the item itself, so two taps racing cannot stack two reports
or resurrect a resolved one.

**3. Triage clears the index key rather than filtering on state.** A resolved report
leaves the queue because it has no key, not because a reader remembered to exclude it —
the same physics as the sparse directory index.

**4. Per (agent, reporter), history is one deep.** Filing again after a resolution
replaces the resolved row. An append-only archive was considered and declined: nothing
reads it, and D15.1 already puts the durable record elsewhere — a report is the *evidence*
for a request-changes or takedown, and that act is separately recorded on the listing.
"""

import hashlib
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from .models import AgentReport, ReportReason
from apis.shared.timestamps import utc_now_iso

logger = logging.getLogger(__name__)

_SK_PREFIX = "REPORT#"

# The single open-queue partition (D15). One partition is correct here and would not be
# for the directory: the queue is bounded by how fast admins work, it is read only by the
# admin console, and it wants one chronological sweep rather than per-agent slices.
_OPEN_PK = "REPORTS#OPEN"

_GSI6_ATTRS = ("GSI6_PK", "GSI6_SK")

# ``state`` is a DynamoDB reserved word; ``reason`` and ``note`` are aliased alongside it
# rather than checked one by one against the reserved list, which grows.
_NAMES = {
    "#st": "state",
    "#reason": "reason",
    "#note": "note",
    "#session": "sessionId",
}


def _now() -> str:
    return utc_now_iso()


def _table():
    import boto3

    table_name = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not table_name:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")
    return boto3.resource("dynamodb").Table(table_name)


def report_id_for(agent_id: str, reporter_id: str) -> str:
    """The deterministic report id for one reporter's report of one Agent (D15.4).

    Hashed rather than the raw reporter id so the sort key is not itself a directory of
    who has reported what — the reporter is admin-visible in the *item*, which is where
    D15.2 puts them, not in a key that any query of the partition would enumerate. The
    agent id is mixed in so the same reporter's ids do not correlate across Agents.
    """
    digest = hashlib.sha256(f"{agent_id}:{reporter_id}".encode()).hexdigest()
    return digest[:16]


def _key(agent_id: str, report_id: str) -> Dict[str, str]:
    return {"PK": f"AST#{agent_id}", "SK": f"{_SK_PREFIX}{report_id}"}


def _to_report(item: Dict[str, Any]) -> AgentReport:
    """Project a raw item into ``AgentReport``, deriving the ids back out of the keys."""
    return AgentReport.model_validate(
        {
            **{k: v for k, v in item.items() if k not in ("PK", "SK", *_GSI6_ATTRS)},
            "reportId": str(item["SK"])[len(_SK_PREFIX) :],
            "agentId": str(item["PK"])[len("AST#") :],
        }
    )


async def submit_report(
    agent_id: str,
    *,
    reporter_id: str,
    reporter_name: str,
    reason: ReportReason,
    note: Optional[str],
    session_id: Optional[str] = None,
) -> Tuple[AgentReport, bool]:
    """File a report, or update this reporter's already-open one (D15.4).

    Returns ``(report, replaced_existing)``. ``replaced_existing`` is True only when an
    *open* report was updated in place — that is the case the UI must not describe as a
    second report having been queued.

    ``session_id`` is the conversation the reporter opted to attach. Like ``note`` it is
    fully replaced on an amendment, never merged: see the REMOVE branch below for why.

    Three conditional writes, no read, in the order the cases actually occur:

    1. **Create** — condition ``attribute_not_exists(SK)``. The common path.
    2. **Update while open** — condition ``state = open``. Keeps ``createdAt`` and the
       index keys, so amending a report does not move it to the front of the sweep.
    3. **Overwrite a closed one** — condition ``state <> open``, with a fresh
       ``createdAt`` and the resolution fields cleared. The guard means this can never
       clobber an open report that step 2 lost a race to.
    """
    from botocore.exceptions import ClientError

    report_id = report_id_for(agent_id, reporter_id)
    now = _now()
    table = _table()

    body: Dict[str, Any] = {
        "reporterId": reporter_id,
        "reporterName": reporter_name,
        "reason": reason,
        "state": "open",
        "updatedAt": now,
    }
    if note:
        body["note"] = note
    # ⚠️ Stored as given. Whether this session belongs to ``reporter_id`` is settled by the
    # caller (``report_service.file_report``) before we get here — this module is storage,
    # and a check duplicated in two places is a check that eventually disagrees with itself.
    if session_id:
        body["sessionId"] = session_id

    def _built(created_at: str) -> AgentReport:
        return AgentReport.model_validate(
            {**body, "reportId": report_id, "agentId": agent_id, "createdAt": created_at}
        )

    # 1. Create.
    try:
        table.put_item(
            Item={
                **_key(agent_id, report_id),
                **body,
                "createdAt": now,
                "GSI6_PK": _OPEN_PK,
                "GSI6_SK": f"CREATED#{now}",
            },
            ConditionExpression="attribute_not_exists(SK)",
        )
        logger.info(f"🚩 {reporter_id} reported agent {agent_id} ({reason})")
        return _built(now), False
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            logger.error(f"Failed to file report on {agent_id}: {e}")
            raise

    # 2. Update the reporter's open report in place, keeping its place in the queue.
    set_parts = ["reporterName = :rname", "#reason = :reason", "#st = :open", "updatedAt = :now"]
    values: Dict[str, Any] = {
        ":rname": reporter_name,
        ":reason": reason,
        ":open": "open",
        ":now": now,
    }
    remove_parts: List[str] = []
    if note:
        set_parts.append("#note = :note")
        values[":note"] = note
    else:
        # An amended report with the text removed must not keep the old text.
        remove_parts.append("#note")

    if session_id:
        set_parts.append("#session = :session")
        values[":session"] = session_id
    else:
        # Same rule, and it matters more here: unticking the box on an amendment is the
        # user *withdrawing* the conversation they previously shared. Leaving the old
        # reference behind would make that consent impossible to take back.
        remove_parts.append("#session")

    expression = "SET " + ", ".join(set_parts)
    if remove_parts:
        expression += " REMOVE " + ", ".join(remove_parts)

    try:
        response = table.update_item(
            Key=_key(agent_id, report_id),
            UpdateExpression=expression,
            ExpressionAttributeNames=_NAMES,
            ExpressionAttributeValues=values,
            ConditionExpression="#st = :open",
            ReturnValues="ALL_NEW",
        )
        logger.info(f"🚩 {reporter_id} updated their open report on agent {agent_id}")
        return _to_report(response["Attributes"]), True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            logger.error(f"Failed to update report on {agent_id}: {e}")
            raise

    # 3. The existing report is resolved or dismissed — this is a genuinely new one.
    table.put_item(
        Item={
            **_key(agent_id, report_id),
            **body,
            "createdAt": now,
            "GSI6_PK": _OPEN_PK,
            "GSI6_SK": f"CREATED#{now}",
        },
        ConditionExpression="#st <> :open",
        ExpressionAttributeNames={"#st": "state"},
        ExpressionAttributeValues={":open": "open"},
    )
    logger.info(f"🚩 {reporter_id} re-reported agent {agent_id} after an earlier resolution")
    return _built(now), False


async def resolve_report(
    agent_id: str,
    report_id: str,
    *,
    state: str,
    resolved_by: str,
    note: Optional[str],
) -> AgentReport:
    """Record a triage decision and drop the report out of the open queue (D15.5).

    ⚠️ This writes **only** the report. It does not touch ``listing.state``: if a report
    warrants delisting, the admin uses the takedown path and that is a separate, recorded
    act. Tying the two together here would make "resolve" quietly mean "delist".

    The ``REMOVE`` of the index keys is what takes it off the queue — not a state filter
    on the read. Raises ``ValueError`` if the report no longer exists.
    """
    from botocore.exceptions import ClientError

    now = _now()
    set_parts = ["#st = :state", "resolvedAt = :now", "resolvedBy = :by", "updatedAt = :now"]
    values: Dict[str, Any] = {":state": state, ":now": now, ":by": resolved_by}
    remove_parts = list(_GSI6_ATTRS)

    if note:
        set_parts.append("resolutionNote = :note")
        values[":note"] = note
    else:
        remove_parts.append("resolutionNote")

    try:
        response = _table().update_item(
            Key=_key(agent_id, report_id),
            UpdateExpression="SET " + ", ".join(set_parts) + " REMOVE " + ", ".join(remove_parts),
            ExpressionAttributeNames={"#st": "state"},
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(SK)",
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ValueError(f"Report not found: {report_id}") from e
        logger.error(f"Failed to resolve report {report_id} on {agent_id}: {e}")
        raise

    logger.info(f"🚩 {resolved_by} marked report {report_id} on {agent_id} as {state}")
    return _to_report(response["Attributes"])


async def list_open_reports(limit: int = 200) -> List[AgentReport]:
    """The open queue, oldest first — a pure GSI6 query (D15).

    Oldest-first because this is a work queue, not a feed: the report that has waited
    longest is the one to triage next. It cannot return a resolved report, because a
    resolved report has no key in this index.
    """
    from boto3.dynamodb.conditions import Key

    response = _table().query(
        IndexName="AgentReportsIndex",
        KeyConditionExpression=Key("GSI6_PK").eq(_OPEN_PK),
        ScanIndexForward=True,
        Limit=limit,
    )
    return [_to_report(item) for item in response.get("Items", [])]


async def count_open_reports() -> int:
    """How many reports await triage — the D10 nav badge.

    ``Select=COUNT`` so the badge never pays to project rows nobody renders.
    """
    from boto3.dynamodb.conditions import Key

    response = _table().query(
        IndexName="AgentReportsIndex",
        KeyConditionExpression=Key("GSI6_PK").eq(_OPEN_PK),
        Select="COUNT",
    )
    return int(response.get("Count", 0))


async def list_reports_for_agent(agent_id: str) -> List[AgentReport]:
    """Every report on one Agent, open or not.

    Not a user-facing read and never joined into any Agent projection: it backs the
    delete sweep, and the admin console's per-agent history.
    """
    from boto3.dynamodb.conditions import Key

    response = _table().query(
        KeyConditionExpression=Key("PK").eq(f"AST#{agent_id}")
        & Key("SK").begins_with(_SK_PREFIX)
    )
    reports = [_to_report(item) for item in response.get("Items", [])]
    reports.sort(key=lambda r: r.created_at, reverse=True)
    return reports


async def delete_reports_for_agent(agent_id: str) -> int:
    """Delete every report on an Agent. Called when the Agent itself is deleted.

    Reports are child rows precisely so they never outlive what they concern — and an
    orphaned *open* report would be worse than untidy: it keeps its sparse index key, so
    it would sit in the admin queue forever pointing at an Agent nobody can open.
    """
    from boto3.dynamodb.conditions import Key

    table = _table()
    response = table.query(
        KeyConditionExpression=Key("PK").eq(f"AST#{agent_id}")
        & Key("SK").begins_with(_SK_PREFIX),
        ProjectionExpression="PK, SK",
    )
    items = response.get("Items", [])
    while "LastEvaluatedKey" in response:
        response = table.query(
            KeyConditionExpression=Key("PK").eq(f"AST#{agent_id}")
            & Key("SK").begins_with(_SK_PREFIX),
            ProjectionExpression="PK, SK",
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))

    if not items:
        return 0

    with table.batch_writer() as batch:
        for item in items:
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})

    logger.info(f"🗑️ Deleted {len(items)} report(s) with agent {agent_id}")
    return len(items)
