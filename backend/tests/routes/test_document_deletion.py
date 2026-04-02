"""Tests for document deletion service functions.

Tests cover:
- soft_delete_document: status transition, TTL, ownership, not-found, idempotency
- hard_delete_document: unconditional delete, no ownership check, error handling
- batch_soft_delete_documents: full batch, partial failures, empty list

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 8.1, 9.1, 9.2
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

ASSISTANT_ID = "ast-001"
DOCUMENT_ID = "DOC-abc123"
OWNER_ID = "user-001"

ENV_PATCH = {"DYNAMODB_ASSISTANTS_TABLE_NAME": "test-table"}


def _dynamo_doc_attrs(
    *,
    status="deleting",
    ttl_value=None,
    chunk_count=5,
    s3_key="assistants/ast-001/documents/DOC-abc123/report.pdf",
):
    """Build a DynamoDB Attributes dict that Document.model_validate can parse."""
    attrs = {
        "PK": f"AST#{ASSISTANT_ID}",
        "SK": f"DOC#{DOCUMENT_ID}",
        "documentId": DOCUMENT_ID,
        "assistantId": ASSISTANT_ID,
        "filename": "report.pdf",
        "contentType": "application/pdf",
        "sizeBytes": 1024,
        "s3Key": s3_key,
        "status": status,
        "chunkCount": chunk_count,
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-06-01T12:00:00Z",
    }
    if ttl_value is not None:
        attrs["ttl"] = ttl_value
    return attrs


def _mock_table():
    """Return a MagicMock that behaves like a DynamoDB Table resource."""
    table = MagicMock()
    table.update_item = MagicMock()
    table.delete_item = MagicMock()
    return table


def _mock_dynamodb_resource(table):
    """Return a MagicMock that behaves like boto3.resource('dynamodb')."""
    resource = MagicMock()
    resource.Table.return_value = table
    return resource


# =========================================================================
# TestSoftDeleteDocument
# =========================================================================


class TestSoftDeleteDocument:
    """Unit tests for soft_delete_document."""

    @pytest.mark.asyncio
    @patch.dict("os.environ", ENV_PATCH)
    async def test_soft_delete_returns_document_with_deleting_status(self):
        """Req 1.1: Soft-delete sets status to 'deleting' and returns the document."""
        ttl_value = int(time.time()) + 7 * 86400
        table = _mock_table()
        table.update_item.return_value = {
            "Attributes": _dynamo_doc_attrs(status="deleting", ttl_value=ttl_value)
        }

        mock_boto3 = MagicMock()
        mock_boto3.resource.return_value = _mock_dynamodb_resource(table)

        with patch("boto3.resource", mock_boto3.resource), \
             patch(
                 "apis.shared.assistants.service.get_assistant",
                 new_callable=AsyncMock,
                 return_value={"assistantId": ASSISTANT_ID},
             ):
            from apis.app_api.documents.services.document_service import (
                soft_delete_document,
            )

            result = await soft_delete_document(ASSISTANT_ID, DOCUMENT_ID, OWNER_ID)

        assert result is not None
        assert result.status == "deleting"
        assert result.document_id == DOCUMENT_ID

    @pytest.mark.asyncio
    @patch.dict("os.environ", ENV_PATCH)
    async def test_soft_delete_sets_ttl(self):
        """Req 1.2: TTL is approximately now + 7*86400."""
        before = int(time.time())
        ttl_value = before + 7 * 86400
        table = _mock_table()
        table.update_item.return_value = {
            "Attributes": _dynamo_doc_attrs(status="deleting", ttl_value=ttl_value)
        }

        mock_boto3 = MagicMock()
        mock_boto3.resource.return_value = _mock_dynamodb_resource(table)

        with patch("boto3.resource", mock_boto3.resource), \
             patch(
                 "apis.shared.assistants.service.get_assistant",
                 new_callable=AsyncMock,
                 return_value={"assistantId": ASSISTANT_ID},
             ):
            from apis.app_api.documents.services.document_service import (
                soft_delete_document,
            )

            result = await soft_delete_document(ASSISTANT_ID, DOCUMENT_ID, OWNER_ID)

        assert result is not None
        assert result.ttl is not None
        expected_ttl = int(time.time()) + 7 * 86400
        assert abs(result.ttl - expected_ttl) < 5

        # Verify update_item was called with TTL in expression values
        call_kwargs = table.update_item.call_args
        expr_values = call_kwargs.kwargs.get("ExpressionAttributeValues", {})
        assert ":ttl_value" in expr_values

    @pytest.mark.asyncio
    @patch.dict("os.environ", ENV_PATCH)
    async def test_soft_delete_returns_none_when_not_found(self):
        """Req 1.5: Returns None when document doesn't exist (ConditionalCheckFailedException)."""
        table = _mock_table()
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}},
            "UpdateItem",
        )

        mock_boto3 = MagicMock()
        mock_boto3.resource.return_value = _mock_dynamodb_resource(table)

        with patch("boto3.resource", mock_boto3.resource), \
             patch(
                 "apis.shared.assistants.service.get_assistant",
                 new_callable=AsyncMock,
                 return_value={"assistantId": ASSISTANT_ID},
             ):
            from apis.app_api.documents.services.document_service import (
                soft_delete_document,
            )

            result = await soft_delete_document(ASSISTANT_ID, DOCUMENT_ID, OWNER_ID)

        assert result is None

    @pytest.mark.asyncio
    @patch.dict("os.environ", ENV_PATCH)
    async def test_soft_delete_returns_none_when_not_owned(self):
        """Req 1.5: Returns None when assistant is not owned by user."""
        with patch(
            "apis.shared.assistants.service.get_assistant",
            new_callable=AsyncMock,
            return_value=None,
        ):
            from apis.app_api.documents.services.document_service import (
                soft_delete_document,
            )

            result = await soft_delete_document(ASSISTANT_ID, DOCUMENT_ID, OWNER_ID)

        assert result is None

    @pytest.mark.asyncio
    @patch.dict("os.environ", ENV_PATCH)
    async def test_soft_delete_idempotent_on_deleting_status(self):
        """Req 1.6: Re-deleting a document already in 'deleting' status succeeds."""
        ttl_value = int(time.time()) + 7 * 86400
        table = _mock_table()
        table.update_item.return_value = {
            "Attributes": _dynamo_doc_attrs(status="deleting", ttl_value=ttl_value)
        }

        mock_boto3 = MagicMock()
        mock_boto3.resource.return_value = _mock_dynamodb_resource(table)

        with patch("boto3.resource", mock_boto3.resource), \
             patch(
                 "apis.shared.assistants.service.get_assistant",
                 new_callable=AsyncMock,
                 return_value={"assistantId": ASSISTANT_ID},
             ):
            from apis.app_api.documents.services.document_service import (
                soft_delete_document,
            )

            result = await soft_delete_document(ASSISTANT_ID, DOCUMENT_ID, OWNER_ID)

        assert result is not None
        assert result.status == "deleting"


# =========================================================================
# TestHardDeleteDocument
# =========================================================================


class TestHardDeleteDocument:
    """Unit tests for hard_delete_document."""

    @pytest.mark.asyncio
    @patch.dict("os.environ", ENV_PATCH)
    async def test_hard_delete_returns_true_on_success(self):
        """Req 9.1: Hard-delete succeeds and returns True."""
        table = _mock_table()
        table.delete_item.return_value = {}

        mock_boto3 = MagicMock()
        mock_boto3.resource.return_value = _mock_dynamodb_resource(table)

        with patch("boto3.resource", mock_boto3.resource):
            from apis.app_api.documents.services.document_service import (
                hard_delete_document,
            )

            result = await hard_delete_document(ASSISTANT_ID, DOCUMENT_ID)

        assert result is True
        table.delete_item.assert_called_once()
        call_kwargs = table.delete_item.call_args
        key = call_kwargs.kwargs.get("Key") or call_kwargs[1].get("Key")
        assert key == {"PK": f"AST#{ASSISTANT_ID}", "SK": f"DOC#{DOCUMENT_ID}"}

    @pytest.mark.asyncio
    @patch.dict("os.environ", ENV_PATCH)
    async def test_hard_delete_no_ownership_check(self):
        """Req 9.2: Hard-delete does NOT call get_assistant (no ownership check)."""
        table = _mock_table()
        table.delete_item.return_value = {}

        mock_boto3 = MagicMock()
        mock_boto3.resource.return_value = _mock_dynamodb_resource(table)

        with patch("boto3.resource", mock_boto3.resource), \
             patch(
                 "apis.shared.assistants.service.get_assistant",
                 new_callable=AsyncMock,
             ) as mock_get_assistant:
            from apis.app_api.documents.services.document_service import (
                hard_delete_document,
            )

            await hard_delete_document(ASSISTANT_ID, DOCUMENT_ID)
            mock_get_assistant.assert_not_called()

    @pytest.mark.asyncio
    @patch.dict("os.environ", ENV_PATCH)
    async def test_hard_delete_returns_false_on_error(self):
        """Req 9.1: Hard-delete returns False on ClientError."""
        table = _mock_table()
        table.delete_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "boom"}},
            "DeleteItem",
        )

        mock_boto3 = MagicMock()
        mock_boto3.resource.return_value = _mock_dynamodb_resource(table)

        with patch("boto3.resource", mock_boto3.resource):
            from apis.app_api.documents.services.document_service import (
                hard_delete_document,
            )

            result = await hard_delete_document(ASSISTANT_ID, DOCUMENT_ID)

        assert result is False


# =========================================================================
# TestBatchSoftDeleteDocuments
# =========================================================================


class TestBatchSoftDeleteDocuments:
    """Unit tests for batch_soft_delete_documents."""

    @pytest.mark.asyncio
    @patch.dict("os.environ", ENV_PATCH)
    async def test_batch_marks_all_documents(self):
        """Req 8.1: All documents in the batch are marked as deleting."""
        table = _mock_table()
        table.update_item.return_value = {}

        mock_boto3 = MagicMock()
        mock_boto3.resource.return_value = _mock_dynamodb_resource(table)

        with patch("boto3.resource", mock_boto3.resource):
            from apis.app_api.documents.services.document_service import (
                batch_soft_delete_documents,
            )

            doc_ids = ["DOC-001", "DOC-002", "DOC-003"]
            count = await batch_soft_delete_documents(ASSISTANT_ID, doc_ids)

        assert count == 3
        assert table.update_item.call_count == 3

    @pytest.mark.asyncio
    @patch.dict("os.environ", ENV_PATCH)
    async def test_batch_skips_missing_documents(self):
        """Req 8.1: Missing documents are skipped, partial count returned."""
        table = _mock_table()
        table.update_item.side_effect = [
            {},
            ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}},
                "UpdateItem",
            ),
            {},
        ]

        mock_boto3 = MagicMock()
        mock_boto3.resource.return_value = _mock_dynamodb_resource(table)

        with patch("boto3.resource", mock_boto3.resource):
            from apis.app_api.documents.services.document_service import (
                batch_soft_delete_documents,
            )

            doc_ids = ["DOC-001", "DOC-missing", "DOC-003"]
            count = await batch_soft_delete_documents(ASSISTANT_ID, doc_ids)

        assert count == 2
        assert table.update_item.call_count == 3

    @pytest.mark.asyncio
    @patch.dict("os.environ", ENV_PATCH)
    async def test_batch_empty_list_returns_zero(self):
        """Req 8.1: Empty document list returns 0."""
        table = _mock_table()

        mock_boto3 = MagicMock()
        mock_boto3.resource.return_value = _mock_dynamodb_resource(table)

        with patch("boto3.resource", mock_boto3.resource):
            from apis.app_api.documents.services.document_service import (
                batch_soft_delete_documents,
            )

            count = await batch_soft_delete_documents(ASSISTANT_ID, [])

        assert count == 0
        table.update_item.assert_not_called()
