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


def list_document_items(assistant_id: str) -> list:
    """All DOC# records under an assistant (raw items, paginated query)."""
    from boto3.dynamodb.conditions import Key

    items = []
    kwargs = {
        "KeyConditionExpression": Key("PK").eq(f"AST#{assistant_id}") & Key("SK").begins_with("DOC#"),
    }
    while True:
        response = _table().query(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return items
        kwargs["ExclusiveStartKey"] = last_key


def set_document_miss_count(assistant_id: str, document_id: str, count: int) -> None:
    """Set the re-crawl miss streak on a web page document (0 resets it)."""
    _table().update_item(
        Key={"PK": f"AST#{assistant_id}", "SK": f"DOC#{document_id}"},
        UpdateExpression="SET consecutiveMisses = :c",
        ExpressionAttributeValues={":c": count},
    )


def update_document_sync_fields(
    assistant_id: str,
    document_id: str,
    *,
    source_etag: Optional[str] = None,
    content_hash: Optional[str] = None,
    previous_chunk_count: Optional[int] = None,
    last_synced_at: Optional[str] = None,
    sync_policy_id: Optional[str] = None,
    staged_content_hash: Optional[str] = None,
) -> None:
    """Targeted update of the sync-bookkeeping fields on a document record.

    Only sets the fields passed — safe alongside the ingestion pipeline's
    own targeted UpdateExpressions (which never touch these attributes).

    ``staged_content_hash`` is for a sync that is about to overwrite the
    document's S3 object with changed bytes, and must be written BEFORE the
    overwrite. A managed knowledge base's ingestion consumer reads it to tell
    the overwrite's event from a redelivery of the original upload's
    (``kb_migration.ingestion_consumer.staged_version_to_reingest``); without
    it the changed bytes are never re-ingested. The legacy pipeline ignores it.
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
    if sync_policy_id is not None:
        set_parts.append("syncPolicyId = :spid")
        values[":spid"] = sync_policy_id
    if staged_content_hash is not None:
        set_parts.append("stagedContentHash = :staged")
        values[":staged"] = staged_content_hash
    if not set_parts:
        return

    _table().update_item(
        Key={"PK": f"AST#{assistant_id}", "SK": f"DOC#{document_id}"},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeValues=values,
    )


def rollback_document_sync_fields(
    assistant_id: str,
    document_id: str,
    *,
    written: Dict[str, str],
    previous: Dict[str, Optional[str]],
) -> bool:
    """Undo a pre-stage sync write whose S3 stage then failed.

    ``written`` maps each attribute to the value the sync just wrote;
    ``previous`` maps it to the value it held before (``None`` = absent).
    The change-detection gates (``sourceEtag``, ``contentHash``) have to be
    written before staging only because they travel with the fields that
    must precede the ObjectCreated event, so a stage that never happened
    must not leave them advanced — the next run would match them and never
    stage the change. ``stagedContentHash`` goes back too: left behind, it
    names a version the object does not hold.

    Conditioned on every attribute still holding what was written, so a
    newer write (another run, the consumer clearing the gates) is left
    alone. Returns False when that condition refused the rollback.
    """
    from botocore.exceptions import ClientError

    set_parts = []
    remove_parts = []
    conditions = []
    names: Dict[str, str] = {}
    values: Dict[str, Any] = {}
    for i, (attribute, value) in enumerate(written.items()):
        names[f"#a{i}"] = attribute
        values[f":w{i}"] = value
        conditions.append(f"#a{i} = :w{i}")
        prior = previous.get(attribute)
        if prior is None:
            remove_parts.append(f"#a{i}")
        else:
            set_parts.append(f"#a{i} = :p{i}")
            values[f":p{i}"] = prior
    if not conditions:
        return True

    expression = []
    if set_parts:
        expression.append("SET " + ", ".join(set_parts))
    if remove_parts:
        expression.append("REMOVE " + ", ".join(remove_parts))
    try:
        _table().update_item(
            Key={"PK": f"AST#{assistant_id}", "SK": f"DOC#{document_id}"},
            UpdateExpression=" ".join(expression),
            ConditionExpression=" AND ".join(conditions),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        logger.info(f"Document {document_id}'s sync fields changed since the failed stage; not rolling back")
        return False
    return True


def clear_document_sync_policy_id(assistant_id: str, document_id: str) -> None:
    """Remove the SyncPolicy back-pointer when its policy is deleted."""
    _table().update_item(
        Key={"PK": f"AST#{assistant_id}", "SK": f"DOC#{document_id}"},
        UpdateExpression="REMOVE syncPolicyId",
    )
