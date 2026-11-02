"""DynamoDB access for quota requests and the request policy (spec §7.1, §7.5).

``PK=QUOTA_REQUEST#{id}`` with the queue on GSI1 (``QUOTA_REQUEST#STATUS#{status}``,
oldest first) and a user's own requests on GSI2 (``QUOTA_REQUEST#USER#{userId}``).
The policy is one row, ``PK=QUOTA_POLICY#REQUESTS``, read through the config
cache because every dialog open and every submit validates against it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from typing import List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

from apis.shared.caching import config_cache

from .models import QuotaRequest, QuotaRequestPolicy

logger = logging.getLogger(__name__)

QUOTA_REQUEST_POLICY_CACHE_KEY = "quota_request_policy"
_POLICY_KEY = {"PK": "QUOTA_POLICY#REQUESTS", "SK": "METADATA"}
_KEY_ATTRS = ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK")


class RequestStateConflict(Exception):
    """The request changed state between read and write (another admin acted)."""


def _encode_cursor(key: Optional[dict]) -> Optional[str]:
    if not key:
        return None
    return base64.urlsafe_b64encode(json.dumps(key).encode()).decode()


def _decode_cursor(cursor: Optional[str]) -> Optional[dict]:
    if not cursor:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except (ValueError, TypeError):
        return None


class QuotaRequestRepository:
    def __init__(self, table_name: Optional[str] = None):
        if table_name is None:
            from agents.main_agent.config.constants import Defaults, EnvVars

            table_name = os.getenv(EnvVars.DYNAMODB_QUOTA_TABLE, Defaults.DYNAMODB_QUOTA_TABLE)
        self.table = boto3.resource("dynamodb").Table(table_name)

    # ── requests ────────────────────────────────────────────────────────

    @staticmethod
    def _to_item(request: QuotaRequest) -> dict:
        sort = f"{request.created_at}#{request.request_id}"
        return {
            "PK": f"QUOTA_REQUEST#{request.request_id}",
            "SK": "METADATA",
            "GSI1PK": f"QUOTA_REQUEST#STATUS#{request.status}",
            "GSI1SK": sort,
            "GSI2PK": f"QUOTA_REQUEST#USER#{request.user_id}",
            "GSI2SK": sort,
            **request.model_dump(by_alias=True, exclude_none=True),
        }

    @staticmethod
    def _from_item(item: dict) -> QuotaRequest:
        return QuotaRequest(**{k: v for k, v in item.items() if k not in _KEY_ATTRS})

    async def create_request(self, request: QuotaRequest) -> QuotaRequest:
        await asyncio.to_thread(
            self.table.put_item,
            Item=self._to_item(request),
            ConditionExpression="attribute_not_exists(PK)",
        )
        return request

    async def save_request(self, request: QuotaRequest, *, expected_status: str) -> QuotaRequest:
        """Replace a request, only if it is still in ``expected_status``.

        Two admins deciding the same request at once: the second write fails
        instead of overwriting the first decision.
        """
        try:
            await asyncio.to_thread(
                self.table.put_item,
                Item=self._to_item(request),
                ConditionExpression="#status = :expected",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":expected": expected_status},
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise RequestStateConflict(request.request_id) from e
            raise
        return request

    async def get_request(self, request_id: str) -> Optional[QuotaRequest]:
        response = await asyncio.to_thread(
            self.table.get_item, Key={"PK": f"QUOTA_REQUEST#{request_id}", "SK": "METADATA"}
        )
        item = response.get("Item")
        return self._from_item(item) if item else None

    async def list_user_requests(self, user_id: str, limit: int = 50) -> List[QuotaRequest]:
        """A user's requests, newest first."""
        response = await asyncio.to_thread(
            self.table.query,
            IndexName="UserAssignmentIndex",
            KeyConditionExpression="GSI2PK = :pk",
            ExpressionAttributeValues={":pk": f"QUOTA_REQUEST#USER#{user_id}"},
            ScanIndexForward=False,
            Limit=limit,
        )
        return [self._from_item(i) for i in response.get("Items", [])]

    async def list_by_status(
        self, status: str, *, limit: int = 50, cursor: Optional[str] = None
    ) -> Tuple[List[QuotaRequest], Optional[str]]:
        """The queue for one status, oldest first."""
        params = {
            "IndexName": "AssignmentTypeIndex",
            "KeyConditionExpression": "GSI1PK = :pk",
            "ExpressionAttributeValues": {":pk": f"QUOTA_REQUEST#STATUS#{status}"},
            "ScanIndexForward": True,
            "Limit": limit,
        }
        start = _decode_cursor(cursor)
        if start:
            params["ExclusiveStartKey"] = start
        response = await asyncio.to_thread(self.table.query, **params)
        items = [self._from_item(i) for i in response.get("Items", [])]
        return items, _encode_cursor(response.get("LastEvaluatedKey"))

    async def count_by_status(self, status: str) -> int:
        params = {
            "IndexName": "AssignmentTypeIndex",
            "KeyConditionExpression": "GSI1PK = :pk",
            "ExpressionAttributeValues": {":pk": f"QUOTA_REQUEST#STATUS#{status}"},
            "Select": "COUNT",
        }
        total = 0
        while True:
            response = await asyncio.to_thread(self.table.query, **params)
            total += int(response.get("Count", 0))
            last = response.get("LastEvaluatedKey")
            if not last:
                return total
            params["ExclusiveStartKey"] = last

    # ── policy ──────────────────────────────────────────────────────────

    async def get_policy(self) -> QuotaRequestPolicy:
        async def _load() -> list:
            response = await asyncio.to_thread(self.table.get_item, Key=_POLICY_KEY)
            item = response.get("Item")
            return [item] if item else []

        items = await config_cache.get_or_load(QUOTA_REQUEST_POLICY_CACHE_KEY, _load)
        if not items:
            return QuotaRequestPolicy()
        return QuotaRequestPolicy(**{k: v for k, v in items[0].items() if k not in ("PK", "SK")})

    async def put_policy(self, policy: QuotaRequestPolicy) -> QuotaRequestPolicy:
        await asyncio.to_thread(
            self.table.put_item,
            Item={**_POLICY_KEY, **policy.model_dump(by_alias=True, exclude_none=True)},
        )
        config_cache.invalidate(QUOTA_REQUEST_POLICY_CACHE_KEY)
        return policy


_repository: Optional[QuotaRequestRepository] = None


def get_quota_request_repository() -> QuotaRequestRepository:
    global _repository
    if _repository is None:
        _repository = QuotaRequestRepository()
    return _repository
