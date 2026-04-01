"""
Unit tests for list documents filtering — verifying that documents
with status="deleting" are excluded from list_assistant_documents results.

Feature: reliable-document-deletion
Requirements: 11.1
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


ASSISTANT_ID = "AST-test-001"
OWNER_ID = "user-abc-123"
TABLE_NAME = "test-table"
ENV_PATCH = {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE_NAME}


def _make_item(doc_id: str, status: str) -> dict:
    """Build a DynamoDB item dict matching the Document model shape."""
    return {
        "PK": f"AST#{ASSISTANT_ID}",
        "SK": f"DOC#{doc_id}",
        "documentId": doc_id,
        "assistantId": ASSISTANT_ID,
        "filename": f"{doc_id}.pdf",
        "contentType": "application/pdf",
        "sizeBytes": 1024,
        "s3Key": f"assistants/{ASSISTANT_ID}/{doc_id}/file.pdf",
        "status": status,
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-06-01T00:00:00Z",
    }


def _setup_mocks(mock_boto3_resource, items):
    """Wire mock boto3.resource to return a table whose query returns *items*."""
    mock_table = MagicMock()
    mock_table.query = MagicMock(return_value={"Items": items})
    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_table
    mock_boto3_resource.return_value = mock_dynamo
    return mock_table


# -----------------------------------------------------------------------
# Requirement 11.1: Mix of statuses — "deleting" excluded
# -----------------------------------------------------------------------


@patch("boto3.resource")
@patch(
    "apis.shared.assistants.service.get_assistant",
    new_callable=AsyncMock,
    return_value=MagicMock(),
)
@patch.dict("os.environ", ENV_PATCH)
@pytest.mark.asyncio
async def test_list_excludes_deleting_documents(mock_get_assistant, mock_boto3_resource):
    """Documents with status='deleting' must not appear in the returned list."""
    items = [
        _make_item("DOC-001", "complete"),
        _make_item("DOC-002", "deleting"),
        _make_item("DOC-003", "uploading"),
        _make_item("DOC-004", "deleting"),
        _make_item("DOC-005", "failed"),
    ]
    _setup_mocks(mock_boto3_resource, items)

    from apis.app_api.documents.services.document_service import (
        list_assistant_documents,
    )

    documents, _ = await list_assistant_documents(ASSISTANT_ID, OWNER_ID)

    returned_ids = {doc.document_id for doc in documents}
    assert returned_ids == {"DOC-001", "DOC-003", "DOC-005"}
    for doc in documents:
        assert doc.status != "deleting"


# -----------------------------------------------------------------------
# Requirement 11.1: All non-deleting documents are returned
# -----------------------------------------------------------------------


@patch("boto3.resource")
@patch(
    "apis.shared.assistants.service.get_assistant",
    new_callable=AsyncMock,
    return_value=MagicMock(),
)
@patch.dict("os.environ", ENV_PATCH)
@pytest.mark.asyncio
async def test_list_returns_all_non_deleting(mock_get_assistant, mock_boto3_resource):
    """Every document NOT in 'deleting' status must be present in the result."""
    items = [
        _make_item("DOC-a", "uploading"),
        _make_item("DOC-b", "chunking"),
        _make_item("DOC-c", "embedding"),
        _make_item("DOC-d", "complete"),
        _make_item("DOC-e", "failed"),
    ]
    _setup_mocks(mock_boto3_resource, items)

    from apis.app_api.documents.services.document_service import (
        list_assistant_documents,
    )

    documents, _ = await list_assistant_documents(ASSISTANT_ID, OWNER_ID)

    returned_ids = {doc.document_id for doc in documents}
    assert returned_ids == {"DOC-a", "DOC-b", "DOC-c", "DOC-d", "DOC-e"}
    assert len(documents) == 5


# -----------------------------------------------------------------------
# Requirement 11.1: All deleting → empty list
# -----------------------------------------------------------------------


@patch("boto3.resource")
@patch(
    "apis.shared.assistants.service.get_assistant",
    new_callable=AsyncMock,
    return_value=MagicMock(),
)
@patch.dict("os.environ", ENV_PATCH)
@pytest.mark.asyncio
async def test_list_empty_when_all_deleting(mock_get_assistant, mock_boto3_resource):
    """When every document is in 'deleting' status, the result must be empty."""
    items = [
        _make_item("DOC-x", "deleting"),
        _make_item("DOC-y", "deleting"),
        _make_item("DOC-z", "deleting"),
    ]
    _setup_mocks(mock_boto3_resource, items)

    from apis.app_api.documents.services.document_service import (
        list_assistant_documents,
    )

    documents, _ = await list_assistant_documents(ASSISTANT_ID, OWNER_ID)

    assert documents == []
