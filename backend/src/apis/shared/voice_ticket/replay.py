"""DynamoDB-backed replay store — single-use ``jti`` enforcement.

Conditional ``PutItem`` with ``attribute_not_exists(jti)`` so the first
attempt wins atomically and any subsequent ``consume`` for the same
``jti`` returns False. The row carries a ``ttl`` attribute matched by the
table's TTL config, so consumed rows are reaped automatically a short
period after the ticket would have expired anyway.

In-memory fallback: when the table name is unset (local dev), the store
keeps a process-local set of consumed jti's. Single-process only — fine
for `uv run python -m main` workflows, not safe for multi-worker dev.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class VoiceTicketReplayStore:
    """Records consumed ``jti`` values with TTL.

    ``consume`` returns True on first use, False if the jti has already
    been recorded. Other failures (connection error, unexpected ClientError)
    propagate so callers fail closed rather than silently letting a ticket
    through.
    """

    def __init__(self, table_name: Optional[str] = None) -> None:
        if table_name is None:
            table_name = os.environ.get("VOICE_TICKET_REPLAY_TABLE_NAME", "")
        self._table_name = table_name
        self._enabled = bool(table_name)

        if self._enabled:
            self._dynamodb = boto3.resource("dynamodb")
            self._table = self._dynamodb.Table(table_name)
            self._fallback_seen: Optional[set[str]] = None
            self._fallback_lock = None
            logger.info("VoiceTicketReplayStore initialized with table: %s", table_name)
        else:
            self._dynamodb = None
            self._table = None
            self._fallback_seen = set()
            self._fallback_lock = threading.Lock()
            logger.debug("VoiceTicketReplayStore in-memory mode (table name unset)")

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def consume(self, jti: str, *, exp: int) -> bool:
        """Record ``jti`` as consumed; return True on first use, False on replay.

        ``exp`` is the ticket's epoch-second expiry; the row's TTL is set a
        short window past it so a replay attempt that races the TTL reaper
        still finds the row.
        """
        if not jti:
            raise ValueError("jti must be non-empty")

        ttl = max(int(exp) + 30, int(time.time()) + 60)

        if not self._enabled:
            assert self._fallback_seen is not None and self._fallback_lock is not None
            with self._fallback_lock:
                if jti in self._fallback_seen:
                    return False
                self._fallback_seen.add(jti)
            return True

        try:
            self._table.put_item(
                Item={"jti": jti, "ttl": ttl},
                ConditionExpression="attribute_not_exists(jti)",
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise


_default_store: Optional[VoiceTicketReplayStore] = None


def get_default_store() -> VoiceTicketReplayStore:
    """Process-wide singleton. First call constructs the boto3 client lazily."""
    global _default_store
    if _default_store is None:
        _default_store = VoiceTicketReplayStore()
    return _default_store


def _reset_default_store_for_tests() -> None:
    global _default_store
    _default_store = None
