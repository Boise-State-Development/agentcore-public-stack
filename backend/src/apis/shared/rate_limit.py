"""Shared sliding-window rate limiter backed by DynamoDB.

Uses atomic counters on the API keys table to enforce per-key request
rate limits.  TTL auto-cleans expired window items.

Fail-open: any DynamoDB error returns *allowed* so a rate-limit outage
never blocks legitimate traffic.
"""

import asyncio
import logging
import os
import time
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class RateLimiter:
    """Sliding-window rate limiter using DynamoDB atomic counters."""

    def __init__(self, table_name: Optional[str] = None):
        self.dynamodb = boto3.resource("dynamodb")
        self.table_name = table_name or os.environ.get(
            "DYNAMODB_API_KEYS_TABLE_NAME", "ApiKeys"
        )
        self.table = self.dynamodb.Table(self.table_name)

    async def check_rate_limit(
        self,
        key_id: str,
        window_seconds: int = 60,
        max_requests: int = 60,
    ) -> bool:
        """Check whether a request is allowed under the rate limit.

        Stores a counter item per key per time window.  TTL auto-cleans
        expired windows.  Fail-open: returns ``True`` on any error.

        The DynamoDB call runs on a worker thread: boto3 is synchronous, and
        app-api is one uvicorn process, so a call made here directly would
        hold the event loop for the round trip on every API-key request.

        Returns ``True`` if the request is allowed, ``False`` if rate-limited.
        """
        now = int(time.time())
        window_key = now // window_seconds

        try:
            resp = await asyncio.to_thread(
                self.table.update_item,
                Key={"PK": f"RATE#{key_id}", "SK": f"WIN#{window_key}"},
                UpdateExpression=(
                    "SET #cnt = if_not_exists(#cnt, :zero) + :one, #ttl = :ttl"
                ),
                ExpressionAttributeNames={"#cnt": "requestCount", "#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":zero": 0,
                    ":one": 1,
                    ":ttl": now + (window_seconds * 2),
                },
                ReturnValues="UPDATED_NEW",
            )
            count = int(resp["Attributes"]["requestCount"])
            return count <= max_requests
        except ClientError as exc:
            logger.warning(f"Rate limit check failed for key {key_id}: {exc}")
            return True  # fail-open


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter
