"""Unit tests for cleanup_service.

Tests cover:
- cleanup_document_resources: retry logic, backoff, success/failure paths, independent phases
- cleanup_assistant_documents: concurrent processing, count consistency

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 8.2, 8.3, 8.4
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ASSISTANT_ID = "ast-001"
DOCUMENT_ID = "DOC-abc123"
S3_KEY = "assistants/ast-001/documents/DOC-abc123/report.pdf"
CHUNK_COUNT = 5

ENV_PATCH = {"S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME": "test-bucket"}


# =========================================================================
# TestCleanupDocumentResources
# =========================================================================


class TestCleanupDocumentResources:
    """Unit tests for cleanup_document_resources."""

    @pytest.mark.asyncio
    @patch.dict("os.environ", ENV_PATCH)
    async def test_cleanup_returns_true_when_both_succeed(self):
        """Req 4.3: Returns True when both vector and S3 deletion succeed."""
        mock_s3_client = MagicMock()
        mock_s3_client.delete_object = MagicMock(return_value={})
        mock_hard_delete = AsyncMock()

        with (
            patch(
                "apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document_deterministic",
                new_callable=AsyncMock,
            ),
            patch(
                "apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document",
                new_callable=AsyncMock,
            ),
            patch("boto3.client", return_value=mock_s3_client),
            patch(
                "apis.app_api.documents.services.document_service.hard_delete_document",
                mock_hard_delete,
            ),
        ):
            from apis.app_api.documents.services.cleanup_service import (
                cleanup_document_resources,
            )

            result = await cleanup_document_resources(
                document_id=DOCUMENT_ID,
                assistant_id=ASSISTANT_ID,
                s3_key=S3_KEY,
                chunk_count=CHUNK_COUNT,
            )

        assert result is True

    @pytest.mark.asyncio
    @patch.dict("os.environ", ENV_PATCH)
    async def test_cleanup_returns_false_when_vectors_fail(self):
        """Req 4.4: Returns False when vector deletion always fails."""
        mock_s3_client = MagicMock()
        mock_s3_client.delete_object = MagicMock(return_value={})
        mock_hard_delete = AsyncMock()

        with (
            patch(
                "apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document_deterministic",
                new_callable=AsyncMock,
                side_effect=Exception("vector error"),
            ),
            patch(
                "apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document",
                new_callable=AsyncMock,
                side_effect=Exception("vector fallback error"),
            ),
            patch("boto3.client", return_value=mock_s3_client),
            patch(
                "apis.app_api.documents.services.cleanup_service.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            patch(
                "apis.app_api.documents.services.document_service.hard_delete_document",
                mock_hard_delete,
            ),
        ):
            from apis.app_api.documents.services.cleanup_service import (
                cleanup_document_resources,
            )

            result = await cleanup_document_resources(
                document_id=DOCUMENT_ID,
                assistant_id=ASSISTANT_ID,
                s3_key=S3_KEY,
                chunk_count=CHUNK_COUNT,
            )

        assert result is False

    @pytest.mark.asyncio
    @patch.dict("os.environ", ENV_PATCH)
    async def test_cleanup_returns_false_when_s3_fails(self):
        """Req 4.4: Returns False when S3 deletion always fails."""
        mock_s3_client = MagicMock()
        mock_s3_client.delete_object = MagicMock(side_effect=Exception("s3 error"))
        mock_hard_delete = AsyncMock()

        with (
            patch(
                "apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document_deterministic",
                new_callable=AsyncMock,
            ),
            patch(
                "apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document",
                new_callable=AsyncMock,
            ),
            patch("boto3.client", return_value=mock_s3_client),
            patch(
                "apis.app_api.documents.services.cleanup_service.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            patch(
                "apis.app_api.documents.services.document_service.hard_delete_document",
                mock_hard_delete,
            ),
        ):
            from apis.app_api.documents.services.cleanup_service import (
                cleanup_document_resources,
            )

            result = await cleanup_document_resources(
                document_id=DOCUMENT_ID,
                assistant_id=ASSISTANT_ID,
                s3_key=S3_KEY,
                chunk_count=CHUNK_COUNT,
            )

        assert result is False

    @pytest.mark.asyncio
    @patch.dict("os.environ", ENV_PATCH)
    async def test_cleanup_independent_phases(self):
        """Req 4.5: S3 deletion is still attempted even when vector deletion fails."""
        mock_s3_client = MagicMock()
        mock_s3_client.delete_object = MagicMock(return_value={})
        mock_hard_delete = AsyncMock()

        with (
            patch(
                "apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document_deterministic",
                new_callable=AsyncMock,
                side_effect=Exception("vector error"),
            ),
            patch(
                "apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document",
                new_callable=AsyncMock,
                side_effect=Exception("vector fallback error"),
            ),
            patch("boto3.client", return_value=mock_s3_client),
            patch(
                "apis.app_api.documents.services.cleanup_service.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            patch(
                "apis.app_api.documents.services.document_service.hard_delete_document",
                mock_hard_delete,
            ),
        ):
            from apis.app_api.documents.services.cleanup_service import (
                cleanup_document_resources,
            )

            await cleanup_document_resources(
                document_id=DOCUMENT_ID,
                assistant_id=ASSISTANT_ID,
                s3_key=S3_KEY,
                chunk_count=CHUNK_COUNT,
            )

        # S3 delete_object was still called despite vector failure
        mock_s3_client.delete_object.assert_called()

    @pytest.mark.asyncio
    @patch.dict("os.environ", ENV_PATCH)
    async def test_cleanup_retries_on_failure(self):
        """Req 4.1: Vector deletion retries up to max_retries (3 calls total)."""
        call_count = 0

        async def fail_twice_then_succeed(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise Exception("transient error")

        mock_s3_client = MagicMock()
        mock_s3_client.delete_object = MagicMock(return_value={})
        mock_hard_delete = AsyncMock()

        with (
            patch(
                "apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document_deterministic",
                side_effect=fail_twice_then_succeed,
            ),
            patch(
                "apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document",
                new_callable=AsyncMock,
            ),
            patch("boto3.client", return_value=mock_s3_client),
            patch(
                "apis.app_api.documents.services.cleanup_service.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            patch(
                "apis.app_api.documents.services.document_service.hard_delete_document",
                mock_hard_delete,
            ),
        ):
            from apis.app_api.documents.services.cleanup_service import (
                cleanup_document_resources,
            )

            result = await cleanup_document_resources(
                document_id=DOCUMENT_ID,
                assistant_id=ASSISTANT_ID,
                s3_key=S3_KEY,
                chunk_count=CHUNK_COUNT,
                max_retries=3,
            )

        assert result is True
        assert call_count == 3

    @pytest.mark.asyncio
    @patch.dict("os.environ", ENV_PATCH)
    async def test_cleanup_calls_hard_delete_on_success(self):
        """Req 4.3, 9.1: hard_delete_document is called when both phases succeed."""
        mock_s3_client = MagicMock()
        mock_s3_client.delete_object = MagicMock(return_value={})
        mock_hard_delete = AsyncMock()

        with (
            patch(
                "apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document_deterministic",
                new_callable=AsyncMock,
            ),
            patch(
                "apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document",
                new_callable=AsyncMock,
            ),
            patch("boto3.client", return_value=mock_s3_client),
            patch(
                "apis.app_api.documents.services.document_service.hard_delete_document",
                mock_hard_delete,
            ),
        ):
            from apis.app_api.documents.services.cleanup_service import (
                cleanup_document_resources,
            )

            await cleanup_document_resources(
                document_id=DOCUMENT_ID,
                assistant_id=ASSISTANT_ID,
                s3_key=S3_KEY,
                chunk_count=CHUNK_COUNT,
            )

        mock_hard_delete.assert_called_once_with(ASSISTANT_ID, DOCUMENT_ID)

    @pytest.mark.asyncio
    @patch.dict("os.environ", ENV_PATCH)
    async def test_cleanup_does_not_call_hard_delete_on_failure(self):
        """Req 4.4: hard_delete_document is NOT called when cleanup fails."""
        mock_s3_client = MagicMock()
        mock_s3_client.delete_object = MagicMock(side_effect=Exception("s3 error"))
        mock_hard_delete = AsyncMock()

        with (
            patch(
                "apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document_deterministic",
                new_callable=AsyncMock,
                side_effect=Exception("vector error"),
            ),
            patch(
                "apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document",
                new_callable=AsyncMock,
                side_effect=Exception("vector fallback error"),
            ),
            patch("boto3.client", return_value=mock_s3_client),
            patch(
                "apis.app_api.documents.services.cleanup_service.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            patch(
                "apis.app_api.documents.services.document_service.hard_delete_document",
                mock_hard_delete,
            ),
        ):
            from apis.app_api.documents.services.cleanup_service import (
                cleanup_document_resources,
            )

            await cleanup_document_resources(
                document_id=DOCUMENT_ID,
                assistant_id=ASSISTANT_ID,
                s3_key=S3_KEY,
                chunk_count=CHUNK_COUNT,
            )

        mock_hard_delete.assert_not_called()


# =========================================================================
# TestCleanupAssistantDocuments
# =========================================================================


class TestCleanupAssistantDocuments:
    """Unit tests for cleanup_assistant_documents."""

    @pytest.mark.asyncio
    async def test_bulk_cleanup_all_succeed(self):
        """Req 8.2, 8.3, 8.4: All 3 documents succeed → (3, 0)."""
        documents = []
        for i in range(3):
            doc = MagicMock()
            doc.document_id = f"DOC-{i}"
            doc.s3_key = f"assistants/{ASSISTANT_ID}/{i}/file.pdf"
            doc.chunk_count = 5
            documents.append(doc)

        async def mock_cleanup(**kwargs):
            return True

        with patch(
            "apis.app_api.documents.services.cleanup_service.cleanup_document_resources",
            side_effect=mock_cleanup,
        ):
            from apis.app_api.documents.services.cleanup_service import (
                cleanup_assistant_documents,
            )

            success, failure = await cleanup_assistant_documents(
                assistant_id=ASSISTANT_ID,
                documents=documents,
            )

        assert (success, failure) == (3, 0)

    @pytest.mark.asyncio
    async def test_bulk_cleanup_mixed_results(self):
        """Req 8.3: 2 succeed, 1 fails → (2, 1)."""
        documents = []
        for i in range(3):
            doc = MagicMock()
            doc.document_id = f"DOC-{i}"
            doc.s3_key = f"assistants/{ASSISTANT_ID}/{i}/file.pdf"
            doc.chunk_count = 5
            documents.append(doc)

        call_idx = 0

        async def mock_cleanup(**kwargs):
            nonlocal call_idx
            call_idx += 1
            # Third document fails
            return call_idx != 3

        with patch(
            "apis.app_api.documents.services.cleanup_service.cleanup_document_resources",
            side_effect=mock_cleanup,
        ):
            from apis.app_api.documents.services.cleanup_service import (
                cleanup_assistant_documents,
            )

            success, failure = await cleanup_assistant_documents(
                assistant_id=ASSISTANT_ID,
                documents=documents,
            )

        assert (success, failure) == (2, 1)

    @pytest.mark.asyncio
    async def test_bulk_cleanup_empty_list(self):
        """Req 8.3: Empty list → (0, 0)."""
        from apis.app_api.documents.services.cleanup_service import (
            cleanup_assistant_documents,
        )

        success, failure = await cleanup_assistant_documents(
            assistant_id=ASSISTANT_ID,
            documents=[],
        )

        assert (success, failure) == (0, 0)
