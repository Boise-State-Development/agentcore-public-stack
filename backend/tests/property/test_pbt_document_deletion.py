"""
Property-based tests for reliable document deletion.

Feature: reliable-document-deletion
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings, strategies as st

from apis.app_api.documents.models import Document


# ---------------------------------------------------------------------------
# Shared Hypothesis strategies
# ---------------------------------------------------------------------------

# Valid pre-delete statuses (any status a document can be in before soft-delete)
st_pre_delete_status = st.sampled_from(
    ["uploading", "chunking", "embedding", "complete", "failed"]
)

# TTL days: positive integers, capped at a reasonable max
st_ttl_days = st.integers(min_value=1, max_value=365)

# Simple ID strategies
st_assistant_id = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-",
    min_size=3,
    max_size=20,
).map(lambda s: f"AST-{s}")

st_document_id = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
    min_size=3,
    max_size=12,
).map(lambda s: f"DOC-{s}")

st_owner_id = st.uuids().map(str)

st_s3_key = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789/-_.",
    min_size=5,
    max_size=80,
)

st_chunk_count = st.one_of(st.none(), st.integers(min_value=0, max_value=500))


# ---------------------------------------------------------------------------
# Property 1: Soft-delete postconditions
# ---------------------------------------------------------------------------


@given(
    status=st_pre_delete_status,
    ttl_days=st_ttl_days,
    assistant_id=st_assistant_id,
    document_id=st_document_id,
    owner_id=st_owner_id,
    s3_key=st_s3_key,
    chunk_count=st_chunk_count,
)
@settings(max_examples=100)
@pytest.mark.asyncio
async def test_soft_delete_postconditions(
    status, ttl_days, assistant_id, document_id, owner_id, s3_key, chunk_count
):
    """
    **Validates: Requirements 1.1, 1.2, 1.3, 1.4**

    For any valid document status and ttl_days > 0, verify the returned document
    has status="deleting", TTL = now_epoch + ttl_days * 86400, updated updatedAt,
    and preserved chunk_count / s3_key.
    """
    # Capture time just before the call for TTL tolerance check
    time_before = int(time.time())

    # Build the DynamoDB ALL_NEW response that update_item would return.
    # The function sets status, updatedAt, and ttl; everything else is preserved.
    def fake_update_item(**kwargs):
        expr_values = kwargs["ExpressionAttributeValues"]
        return {
            "Attributes": {
                "PK": f"AST#{assistant_id}",
                "SK": f"DOC#{document_id}",
                "documentId": document_id,
                "assistantId": assistant_id,
                "filename": "test.pdf",
                "contentType": "application/pdf",
                "sizeBytes": 1024,
                "s3Key": s3_key,
                "status": expr_values[":deleting"],
                "chunkCount": chunk_count,
                "createdAt": "2024-01-01T00:00:00Z",
                "updatedAt": expr_values[":now"],
                "ttl": expr_values[":ttl_value"],
            }
        }

    # Mock boto3.resource inside the function's scope
    mock_table = MagicMock()
    mock_table.update_item = MagicMock(side_effect=fake_update_item)

    mock_dynamodb = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    mock_boto3 = MagicMock()
    mock_boto3.resource.return_value = mock_dynamodb

    with (
        patch(
            "apis.shared.assistants.service.get_assistant",
            new_callable=AsyncMock,
            return_value=MagicMock(),  # ownership check passes
        ),
        patch.dict(
            "os.environ",
            {"DYNAMODB_ASSISTANTS_TABLE_NAME": "test-table"},
        ),
        patch(
            "boto3.resource",
            return_value=mock_dynamodb,
        ),
    ):
        from apis.app_api.documents.services.document_service import (
            soft_delete_document,
        )

        result = await soft_delete_document(
            assistant_id=assistant_id,
            document_id=document_id,
            owner_id=owner_id,
            ttl_days=ttl_days,
        )

    time_after = int(time.time())

    # Postcondition checks
    assert result is not None, "soft_delete_document should return a Document"
    assert isinstance(result, Document)

    # 1.1: status is "deleting"
    assert result.status == "deleting"

    # 1.2: TTL = now_epoch + ttl_days * 86400 (within tolerance of call duration)
    expected_ttl_min = time_before + ttl_days * 86400
    expected_ttl_max = time_after + ttl_days * 86400
    assert expected_ttl_min <= result.ttl <= expected_ttl_max, (
        f"TTL {result.ttl} not in expected range [{expected_ttl_min}, {expected_ttl_max}]"
    )

    # 1.3: updatedAt is refreshed (non-empty ISO timestamp)
    assert result.updated_at is not None
    assert len(result.updated_at) > 0
    assert result.updated_at != "2024-01-01T00:00:00Z", (
        "updatedAt should be refreshed, not the original value"
    )

    # 1.4: chunk_count and s3_key are preserved
    assert result.s3_key == s3_key
    assert result.chunk_count == chunk_count


# ---------------------------------------------------------------------------
# Property 2: Idempotent soft-delete
# ---------------------------------------------------------------------------


@given(
    ttl_days=st_ttl_days,
    assistant_id=st_assistant_id,
    document_id=st_document_id,
    owner_id=st_owner_id,
    s3_key=st_s3_key,
    chunk_count=st_chunk_count,
)
@settings(max_examples=100)
@pytest.mark.asyncio
async def test_idempotent_soft_delete(
    ttl_days, assistant_id, document_id, owner_id, s3_key, chunk_count
):
    """
    **Validates: Requirement 1.6**

    For any document already in "deleting" status, calling soft_delete_document
    again shall succeed without error and the document shall remain in "deleting"
    status.
    """
    import time as _time

    # Build the DynamoDB ALL_NEW response simulating a document already in
    # "deleting" status.  The update_item call should still succeed because
    # the ConditionExpression only checks attribute_exists(PK).
    def fake_update_item(**kwargs):
        expr_values = kwargs["ExpressionAttributeValues"]
        return {
            "Attributes": {
                "PK": f"AST#{assistant_id}",
                "SK": f"DOC#{document_id}",
                "documentId": document_id,
                "assistantId": assistant_id,
                "filename": "already-deleting.pdf",
                "contentType": "application/pdf",
                "sizeBytes": 2048,
                "s3Key": s3_key,
                "status": expr_values[":deleting"],  # stays "deleting"
                "chunkCount": chunk_count,
                "createdAt": "2024-01-01T00:00:00Z",
                "updatedAt": expr_values[":now"],
                "ttl": expr_values[":ttl_value"],
            }
        }

    mock_table = MagicMock()
    mock_table.update_item = MagicMock(side_effect=fake_update_item)

    mock_dynamodb = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    with (
        patch(
            "apis.shared.assistants.service.get_assistant",
            new_callable=AsyncMock,
            return_value=MagicMock(),  # ownership check passes
        ),
        patch.dict(
            "os.environ",
            {"DYNAMODB_ASSISTANTS_TABLE_NAME": "test-table"},
        ),
        patch(
            "boto3.resource",
            return_value=mock_dynamodb,
        ),
    ):
        from apis.app_api.documents.services.document_service import (
            soft_delete_document,
        )

        result = await soft_delete_document(
            assistant_id=assistant_id,
            document_id=document_id,
            owner_id=owner_id,
            ttl_days=ttl_days,
        )

    # The call must succeed (not None) — idempotent re-delete
    assert result is not None, (
        "soft_delete_document on an already-deleting document should succeed"
    )
    assert isinstance(result, Document)

    # Status must still be "deleting"
    assert result.status == "deleting", (
        f"Expected status='deleting', got '{result.status}'"
    )


# ---------------------------------------------------------------------------
# Property 8: Bulk soft-delete covers all documents
# ---------------------------------------------------------------------------


@given(
    assistant_id=st_assistant_id,
    document_ids=st.lists(st_document_id, min_size=0, max_size=20),
    ttl_days=st_ttl_days,
)
@settings(max_examples=100)
@pytest.mark.asyncio
async def test_bulk_soft_delete_covers_all_documents(
    assistant_id, document_ids, ttl_days
):
    """
    **Validates: Requirements 8.1**

    For any list of N document IDs, batch_soft_delete_documents marks all N
    as "deleting" with TTL. Verify the returned count equals len(document_ids)
    and update_item was called exactly N times (once per document).
    """
    mock_table = MagicMock()
    # All update_item calls succeed (no ConditionalCheckFailedException)
    mock_table.update_item = MagicMock(return_value={})

    mock_dynamodb = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    with (
        patch.dict(
            "os.environ",
            {"DYNAMODB_ASSISTANTS_TABLE_NAME": "test-table"},
        ),
        patch(
            "boto3.resource",
            return_value=mock_dynamodb,
        ),
    ):
        from apis.app_api.documents.services.document_service import (
            batch_soft_delete_documents,
        )

        result = await batch_soft_delete_documents(
            assistant_id=assistant_id,
            document_ids=document_ids,
            ttl_days=ttl_days,
        )

    n = len(document_ids)

    # Returned count equals the number of document IDs provided
    assert result == n, (
        f"Expected {n} documents marked, got {result}"
    )

    # update_item was called exactly N times (once per document)
    assert mock_table.update_item.call_count == n, (
        f"Expected {n} update_item calls, got {mock_table.update_item.call_count}"
    )

    # Each call targeted the correct assistant and document
    for i, doc_id in enumerate(document_ids):
        call_kwargs = mock_table.update_item.call_args_list[i][1]
        expected_key = {
            "PK": f"AST#{assistant_id}",
            "SK": f"DOC#{doc_id}",
        }
        assert call_kwargs["Key"] == expected_key, (
            f"Call {i}: expected key {expected_key}, got {call_kwargs['Key']}"
        )

        # Verify the update sets status to "deleting" and includes a TTL
        expr_values = call_kwargs["ExpressionAttributeValues"]
        assert expr_values[":deleting"] == "deleting", (
            f"Call {i}: expected status 'deleting', got {expr_values[':deleting']}"
        )
        assert isinstance(expr_values[":ttl_value"], int), (
            f"Call {i}: TTL should be an integer epoch, got {type(expr_values[':ttl_value'])}"
        )
        assert expr_values[":ttl_value"] > 0, (
            f"Call {i}: TTL should be positive, got {expr_values[':ttl_value']}"
        )


# ---------------------------------------------------------------------------
# Shared strategy for all document statuses (including "deleting")
# ---------------------------------------------------------------------------

st_all_statuses = st.sampled_from(
    ["uploading", "chunking", "embedding", "complete", "failed", "deleting"]
)


# ---------------------------------------------------------------------------
# Property 10: List documents excludes deleting status
# ---------------------------------------------------------------------------


def _make_dynamo_item(assistant_id: str, doc_id: str, status: str) -> dict:
    """Build a DynamoDB item dict matching the Document model shape."""
    return {
        "PK": f"AST#{assistant_id}",
        "SK": f"DOC#{doc_id}",
        "documentId": doc_id,
        "assistantId": assistant_id,
        "filename": f"{doc_id}.pdf",
        "contentType": "application/pdf",
        "sizeBytes": 1024,
        "s3Key": f"assistants/{assistant_id}/{doc_id}/file.pdf",
        "status": status,
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-06-01T00:00:00Z",
    }


@given(
    assistant_id=st_assistant_id,
    owner_id=st_owner_id,
    statuses=st.lists(st_all_statuses, min_size=0, max_size=15),
)
@settings(max_examples=100)
@pytest.mark.asyncio
async def test_list_documents_excludes_deleting_status(
    assistant_id, owner_id, statuses
):
    """
    **Validates: Requirements 11.1**

    For any set of documents with mixed statuses, listing documents SHALL
    never return documents with status="deleting". All non-deleting documents
    SHALL be returned.
    """
    # Build document IDs and DynamoDB Items for each generated status
    doc_ids = [f"DOC-{i:04d}" for i in range(len(statuses))]
    items = [
        _make_dynamo_item(assistant_id, doc_id, status)
        for doc_id, status in zip(doc_ids, statuses)
    ]

    # Mock DynamoDB table.query to return the generated items
    mock_table = MagicMock()
    mock_table.query = MagicMock(return_value={"Items": items})

    mock_dynamodb = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    with (
        patch(
            "apis.shared.assistants.service.get_assistant",
            new_callable=AsyncMock,
            return_value=MagicMock(),  # ownership check passes
        ),
        patch.dict(
            "os.environ",
            {"DYNAMODB_ASSISTANTS_TABLE_NAME": "test-table"},
        ),
        patch(
            "boto3.resource",
            return_value=mock_dynamodb,
        ),
    ):
        from apis.app_api.documents.services.document_service import (
            list_assistant_documents,
        )

        documents, _ = await list_assistant_documents(
            assistant_id=assistant_id,
            owner_id=owner_id,
        )

    # Property: no returned document has status="deleting"
    for doc in documents:
        assert doc.status != "deleting", (
            f"Document {doc.document_id} has status='deleting' but should be excluded"
        )

    # Property: all non-deleting documents ARE returned
    expected_non_deleting = {
        doc_id
        for doc_id, status in zip(doc_ids, statuses)
        if status != "deleting"
    }
    returned_ids = {doc.document_id for doc in documents}
    assert returned_ids == expected_non_deleting, (
        f"Expected non-deleting docs {expected_non_deleting}, got {returned_ids}"
    )
