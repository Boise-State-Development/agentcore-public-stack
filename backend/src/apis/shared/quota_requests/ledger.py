"""DynamoDB access for the chargeback ledger (quota-requests spec §7.3, §9.4).

Charge lines live in the ``user-quotas`` table under ``QUOTA_CHARGE#{period}``
with the sort key ``{fundingKey or "central"}#{userId}#{extensionId}``, so one
period's report is one query that comes back grouped by fund. Four GSIs the
table already has answer the cross-period questions, each under its own
``QUOTA_CHARGE#…`` prefix (assignment reads on those indexes match exact
``ASSIGNMENT_TYPE#`` / ``USER#`` / ``ROLE#`` / ``APP_ROLE#`` partitions, so the
families never meet):

    GSI1 (AssignmentTypeIndex)    QUOTA_CHARGE#FUND#{fundingKey}   {period}#{userId}
    GSI2 (UserAssignmentIndex)    QUOTA_CHARGE#USER#{userId}       {period}#{extensionId}
    GSI3 (RoleAssignmentIndex)    QUOTA_CHARGE#AUTH#{authorizer}   {period}#{userId}
    GSI6 (AppRoleAssignmentIndex) QUOTA_CHARGE#GROUP#{groupValue}  {period}#{fundingKey}

Each period's close state is one marker row in the same partition,
``SK = #CLOSE`` (``#`` sorts before every fund key, which starts with a schema
key or ``central``). The app-api role has no ``TransactWriteItems``, so a close
is a conditional claim on the marker, a batch of lines, and a conditional
finish; a claim abandoned mid-write is taken over once it is stale.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, Iterable, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

from agents.main_agent.quota.models import QuotaExtension

from .models import QuotaChargeLine

logger = logging.getLogger(__name__)

MARKER_SK = "#CLOSE"
FIELD_PREFIX = "f_"

_KEY_ATTRS = frozenset(
    ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK", "GSI3PK", "GSI3SK", "GSI6PK", "GSI6SK")
)
_EXTENSION_KEY_ATTRS = ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK")

# One family per filter that has an index: (index name, partition attr, sort attr, prefix).
INDEX_FAMILIES: Dict[str, Tuple[str, str, str, str]] = {
    "fundingKey": ("AssignmentTypeIndex", "GSI1PK", "GSI1SK", "QUOTA_CHARGE#FUND#"),
    "userId": ("UserAssignmentIndex", "GSI2PK", "GSI2SK", "QUOTA_CHARGE#USER#"),
    "authorizer": ("RoleAssignmentIndex", "GSI3PK", "GSI3SK", "QUOTA_CHARGE#AUTH#"),
    "group": ("AppRoleAssignmentIndex", "GSI6PK", "GSI6SK", "QUOTA_CHARGE#GROUP#"),
}


def _is_conflict(e: ClientError) -> bool:
    return e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def line_sort_key(line: QuotaChargeLine) -> str:
    return f"{line.fund_sort_key}#{line.user_id}#{line.extension_id}"


def line_to_item(line: QuotaChargeLine) -> dict:
    """A line as stored: keys, scalar attributes, and the fields flattened as ``f_<key>``."""
    body = line.model_dump(by_alias=True, exclude_none=True, exclude={"fields"})
    item = {
        "PK": f"QUOTA_CHARGE#{line.period}",
        "SK": line_sort_key(line),
        "GSI2PK": f"QUOTA_CHARGE#USER#{line.user_id}",
        "GSI2SK": f"{line.period}#{line.extension_id}",
        **body,
        **{f"{FIELD_PREFIX}{k}": v for k, v in line.fields.items() if v},
    }
    if line.funding_key:
        item["GSI1PK"] = f"QUOTA_CHARGE#FUND#{line.funding_key}"
        item["GSI1SK"] = f"{line.period}#{line.user_id}"
    if line.authorizer_email:
        item["GSI3PK"] = f"QUOTA_CHARGE#AUTH#{line.authorizer_email.lower()}"
        item["GSI3SK"] = f"{line.period}#{line.user_id}"
    if line.group_value:
        item["GSI6PK"] = f"QUOTA_CHARGE#GROUP#{line.group_value}"
        item["GSI6SK"] = f"{line.period}#{line.fund_sort_key}"
    return item


def item_to_line(item: dict) -> QuotaChargeLine:
    fields = {k[len(FIELD_PREFIX):]: v for k, v in item.items() if k.startswith(FIELD_PREFIX)}
    body = {k: v for k, v in item.items() if k not in _KEY_ATTRS and not k.startswith(FIELD_PREFIX)}
    return QuotaChargeLine(**body, fields=fields)


class ChargeLedgerRepository:
    def __init__(self, table_name: Optional[str] = None):
        if table_name is None:
            from agents.main_agent.config.constants import Defaults, EnvVars

            table_name = os.getenv(EnvVars.DYNAMODB_QUOTA_TABLE, Defaults.DYNAMODB_QUOTA_TABLE)
        self.table_name = table_name
        self._dynamodb = boto3.resource("dynamodb")
        self.table = self._dynamodb.Table(table_name)

    async def _query_all(self, **params) -> List[dict]:
        items: List[dict] = []
        while True:
            response = await asyncio.to_thread(self.table.query, **params)
            items.extend(response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                return items
            params["ExclusiveStartKey"] = last

    # ── extensions in a period (§7.2 pointers) ──────────────────────────

    async def period_extension_ids(self, period: str) -> List[str]:
        items = await self._query_all(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": f"QUOTA_EXT_PERIOD#{period}"},
        )
        return sorted({i["extensionId"] for i in items if i.get("extensionId")})

    async def get_extensions(self, extension_ids: Iterable[str]) -> List[QuotaExtension]:
        """Batch-read extensions; a pointer whose extension never landed is skipped."""
        ids = sorted(set(extension_ids))
        found: List[QuotaExtension] = []
        for start in range(0, len(ids), 100):
            keys = [{"PK": f"QUOTA_EXT#{i}", "SK": "METADATA"} for i in ids[start:start + 100]]
            request = {self.table_name: {"Keys": keys}}
            while request:
                response = await asyncio.to_thread(self._dynamodb.batch_get_item, RequestItems=request)
                for item in response.get("Responses", {}).get(self.table_name, []):
                    found.append(
                        QuotaExtension(**{k: v for k, v in item.items() if k not in _EXTENSION_KEY_ATTRS})
                    )
                request = response.get("UnprocessedKeys") or None
        return found

    # ── lines ───────────────────────────────────────────────────────────

    async def period_lines(self, period: str, *, fund_prefix: Optional[str] = None) -> List[QuotaChargeLine]:
        """A closed period's lines in fund order; ``fund_prefix`` narrows by sort key."""
        if fund_prefix:
            condition = "PK = :pk AND begins_with(SK, :sk)"
            values = {":pk": f"QUOTA_CHARGE#{period}", ":sk": f"{fund_prefix}#"}
        else:
            condition = "PK = :pk"
            values = {":pk": f"QUOTA_CHARGE#{period}"}
        items = await self._query_all(KeyConditionExpression=condition, ExpressionAttributeValues=values)
        return [item_to_line(i) for i in items if i.get("SK") != MARKER_SK]

    async def index_lines(self, family: str, value: str, from_period: str, to_period: str) -> List[QuotaChargeLine]:
        """Lines across closed periods through one GSI family, bounded by period."""
        index, pk_attr, sk_attr, prefix = INDEX_FAMILIES[family]
        if family == "authorizer":
            value = value.lower()
        items = await self._query_all(
            IndexName=index,
            KeyConditionExpression=f"{pk_attr} = :pk AND {sk_attr} BETWEEN :lo AND :hi",
            ExpressionAttributeValues={
                ":pk": f"{prefix}{value}",
                ":lo": from_period,
                ":hi": f"{to_period}#\uffff",
            },
        )
        return [item_to_line(i) for i in items]

    async def fund_lines(self, funding_key: str) -> List[QuotaChargeLine]:
        return await self.index_lines("fundingKey", funding_key, "0000-00", "9999-99")

    async def replace_lines(
        self, period: str, lines: List[QuotaChargeLine], stale_sort_keys: Iterable[str]
    ) -> None:
        """Write a close's lines and delete the previous close's lines it no longer has."""
        keep = {line_sort_key(l) for l in lines}

        def _write() -> None:
            with self.table.batch_writer() as batch:
                for line in lines:
                    batch.put_item(Item=line_to_item(line))
                for sk in stale_sort_keys:
                    if sk not in keep and sk != MARKER_SK:
                        batch.delete_item(Key={"PK": f"QUOTA_CHARGE#{period}", "SK": sk})

        await asyncio.to_thread(_write)

    # ── close marker ────────────────────────────────────────────────────

    async def get_marker(self, period: str) -> Optional[dict]:
        response = await asyncio.to_thread(
            self.table.get_item, Key={"PK": f"QUOTA_CHARGE#{period}", "SK": MARKER_SK}
        )
        return response.get("Item")

    async def claim_first_close(self, period: str, *, now: str, stale_before: str) -> Optional[int]:
        """Claim version 1 of a period's close, or take over one abandoned mid-write.

        Returns the version to write, or None when another close holds or
        finished the period.
        """
        try:
            await asyncio.to_thread(
                self.table.put_item,
                Item={
                    "PK": f"QUOTA_CHARGE#{period}", "SK": MARKER_SK, "period": period,
                    "status": "closing", "closeVersion": 1, "claimedAt": now,
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
            return 1
        except ClientError as e:
            if not _is_conflict(e):
                raise
        return await self._take_over_stale(period, now=now, stale_before=stale_before)

    async def _take_over_stale(self, period: str, *, now: str, stale_before: str) -> Optional[int]:
        marker = await self.get_marker(period)
        if not marker or marker.get("status") != "closing" or marker.get("claimedAt", "") >= stale_before:
            return None
        try:
            await asyncio.to_thread(
                self.table.update_item,
                Key={"PK": f"QUOTA_CHARGE#{period}", "SK": MARKER_SK},
                UpdateExpression="SET claimedAt = :now",
                ConditionExpression="#s = :closing AND claimedAt = :old",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":now": now, ":closing": "closing", ":old": marker["claimedAt"]},
            )
        except ClientError as e:
            if _is_conflict(e):
                return None
            raise
        logger.warning("Took over an abandoned chargeback close", extra={"event": "quota_close_takeover", "period": period})
        return int(marker["closeVersion"])

    async def claim_reclose(self, period: str, *, from_version: int, now: str) -> Optional[int]:
        """Claim the next version of a closed period. None if it moved underneath us."""
        try:
            await asyncio.to_thread(
                self.table.update_item,
                Key={"PK": f"QUOTA_CHARGE#{period}", "SK": MARKER_SK},
                UpdateExpression="SET #s = :closing, closeVersion = :next, claimedAt = :now",
                ConditionExpression="#s = :closed AND closeVersion = :v",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":closing": "closing", ":closed": "closed",
                    ":next": from_version + 1, ":v": from_version, ":now": now,
                },
            )
        except ClientError as e:
            if _is_conflict(e):
                return None
            raise
        return from_version + 1

    async def finish_close(
        self, period: str, *, version: int, claimed_at: str, closed_at: str, closed_by: str, summary: dict
    ) -> bool:
        try:
            await asyncio.to_thread(
                self.table.update_item,
                Key={"PK": f"QUOTA_CHARGE#{period}", "SK": MARKER_SK},
                UpdateExpression=(
                    "SET #s = :closed, closedAt = :at, closedBy = :by, unattributed = :u, lineCount = :n"
                ),
                ConditionExpression="closeVersion = :v AND claimedAt = :claimed",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":closed": "closed", ":at": closed_at, ":by": closed_by,
                    ":u": summary["unattributed"], ":n": summary["lineCount"],
                    ":v": version, ":claimed": claimed_at,
                },
            )
        except ClientError as e:
            if _is_conflict(e):
                return False
            raise
        return True


_ledger: Optional[ChargeLedgerRepository] = None


def get_charge_ledger_repository() -> ChargeLedgerRepository:
    global _ledger
    if _ledger is None:
        _ledger = ChargeLedgerRepository()
    return _ledger
