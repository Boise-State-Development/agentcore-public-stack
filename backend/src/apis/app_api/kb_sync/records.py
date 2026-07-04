"""Raw assistants-table record access for the kb-sync Lambdas.

The dispatcher and worker read/write assistant, document, and crawl
records by their adjacency-list keys instead of importing the app-api
domain services — the assistants package's __init__ drags in the
embeddings stack, and keeping the kb-sync image surface minimal is a
deliberate constraint (see backend/Dockerfile.kb-sync).

The key patterns are the stable storage contract (see
apis/shared/assistants/service.py, documents/services/document_service.py,
web_sources). The kb-sync tests create records through those REAL
services, so any schema drift breaks tests loudly rather than silently
orphaning sync work.
"""

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _table():
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["DYNAMODB_ASSISTANTS_TABLE_NAME"])


def get_item(pk: str, sk: str) -> Optional[Dict[str, Any]]:
    response = _table().get_item(Key={"PK": pk, "SK": sk})
    return response.get("Item")


def get_assistant_item(assistant_id: str) -> Optional[Dict[str, Any]]:
    """Assistant METADATA record (existence + activity timestamps)."""
    return get_item(f"AST#{assistant_id}", "METADATA")


def get_document_item(assistant_id: str, document_id: str) -> Optional[Dict[str, Any]]:
    return get_item(f"AST#{assistant_id}", f"DOC#{document_id}")


def get_source_item(assistant_id: str, source_type: str, source_ref: str) -> Optional[Dict[str, Any]]:
    """The source record backing a sync policy (DOC# or CRAWL#)."""
    sk_prefix = "DOC#" if source_type == "drive_file" else "CRAWL#"
    return get_item(f"AST#{assistant_id}", f"{sk_prefix}{source_ref}")


def update_document_sync_fields(
    assistant_id: str,
    document_id: str,
    *,
    source_etag: Optional[str] = None,
    content_hash: Optional[str] = None,
    previous_chunk_count: Optional[int] = None,
    last_synced_at: Optional[str] = None,
) -> None:
    """Targeted update of the sync-bookkeeping fields on a document record.

    Only sets the fields passed — safe alongside the ingestion pipeline's
    own targeted UpdateExpressions (which never touch these attributes).
    """
    set_parts = []
    values: Dict[str, Any] = {}
    if source_etag is not None:
        set_parts.append("sourceEtag = :etag")
        values[":etag"] = source_etag
    if content_hash is not None:
        set_parts.append("contentHash = :hash")
        values[":hash"] = content_hash
    if previous_chunk_count is not None:
        set_parts.append("previousChunkCount = :prev")
        values[":prev"] = previous_chunk_count
    if last_synced_at is not None:
        set_parts.append("lastSyncedAt = :synced")
        values[":synced"] = last_synced_at
    if not set_parts:
        return

    _table().update_item(
        Key={"PK": f"AST#{assistant_id}", "SK": f"DOC#{document_id}"},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeValues=values,
    )
