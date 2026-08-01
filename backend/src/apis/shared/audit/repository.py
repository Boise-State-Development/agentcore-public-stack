"""DynamoDB persistence for audit records.

Three access patterns, one per index:

  * **history of a target** — base table, ``PK = AUDIT#<type>#<id>``
  * **actions by an admin** — ``ActorIndex``
  * **recent activity**     — ``RecentIndex``, month-sharded (see
    :pyattr:`AuditRecord.recent_pk`)

All three read newest-first (``ScanIndexForward=False``), because every question
an audit log answers starts with "what happened lately".
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

from .models import AuditRecord

logger = logging.getLogger(__name__)

ACTOR_INDEX = "ActorIndex"
RECENT_INDEX = "RecentIndex"

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class AuditRepository:
    """Repository for audit-record writes and reads."""

    def __init__(self, table_name: Optional[str] = None):
        self.table_name = table_name or os.environ.get(
            "DYNAMODB_AUDIT_LOG_TABLE_NAME", "audit-log"
        )
        self._dynamodb = boto3.resource("dynamodb")
        self._table = self._dynamodb.Table(self.table_name)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def put(self, record: AuditRecord) -> None:
        """Persist one record.

        Raises on failure. Callers reach this through
        :meth:`AuditService.record`, which is the layer that decides an audit
        write must never fail the mutation it describes.
        """
        self._table.put_item(Item=record.to_item())

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def _query(
        self,
        *,
        key_expression,
        index_name: Optional[str],
        limit: int,
        cursor: Optional[Dict[str, Any]],
    ) -> Tuple[List[AuditRecord], Optional[Dict[str, Any]]]:
        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": key_expression,
            "ScanIndexForward": False,
            "Limit": max(1, min(limit, MAX_PAGE_SIZE)),
        }
        if index_name:
            kwargs["IndexName"] = index_name
        if cursor:
            kwargs["ExclusiveStartKey"] = cursor

        try:
            response = self._table.query(**kwargs)
        except ClientError as e:
            logger.error(f"Audit query failed on {self.table_name}: {e}")
            raise

        records = [AuditRecord.from_item(i) for i in response.get("Items", [])]
        return records, response.get("LastEvaluatedKey")

    def list_recent(
        self,
        month: str,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[AuditRecord], Optional[Dict[str, Any]]]:
        """Records written in ``month`` (``YYYY-MM``), newest first."""
        from boto3.dynamodb.conditions import Key

        return self._query(
            key_expression=Key("GSI2PK").eq(f"AUDIT#{month}"),
            index_name=RECENT_INDEX,
            limit=limit,
            cursor=cursor,
        )

    def list_for_target(
        self,
        target_type: str,
        target_id: str,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[AuditRecord], Optional[Dict[str, Any]]]:
        """Full history of one target, newest first."""
        from boto3.dynamodb.conditions import Key

        return self._query(
            key_expression=Key("PK").eq(f"AUDIT#{target_type}#{target_id}"),
            index_name=None,
            limit=limit,
            cursor=cursor,
        )

    def list_for_actor(
        self,
        actor_user_id: str,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[AuditRecord], Optional[Dict[str, Any]]]:
        """Everything one admin did, newest first."""
        from boto3.dynamodb.conditions import Key

        return self._query(
            key_expression=Key("GSI1PK").eq(f"ACTOR#{actor_user_id}"),
            index_name=ACTOR_INDEX,
            limit=limit,
            cursor=cursor,
        )
