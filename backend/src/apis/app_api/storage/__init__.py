"""Storage utilities for DynamoDB-backed persistence"""

import logging
import os

from .metadata_storage import MetadataStorage
from .dynamodb_storage import DynamoDBStorage

logger = logging.getLogger(__name__)


def get_metadata_storage() -> MetadataStorage:
    """
    Get DynamoDB storage backend.

    Environment Variables:
        DYNAMODB_SESSIONS_METADATA_TABLE_NAME: DynamoDB table name for message metadata
        DYNAMODB_COST_SUMMARY_TABLE_NAME: DynamoDB table for cost summaries
    """
    sessions_table = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
    cost_summary_table = os.environ.get("DYNAMODB_COST_SUMMARY_TABLE_NAME")

    logger.info(
        "Using DynamoDB metadata storage - "
        "sessions_table=%s, cost_summary_table=%s",
        sessions_table, cost_summary_table,
    )
    return DynamoDBStorage()


__all__ = [
    "MetadataStorage",
    "get_metadata_storage",
    "DynamoDBStorage",
]
