"""
Unit tests for deterministic vector deletion.

Feature: reliable-document-deletion
Requirements: 5.1, 5.2, 5.3
"""

import importlib
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def mock_s3vectors():
    """Provide a mock s3vectors client and reload the module with patched env vars."""
    mock_client = MagicMock()
    mock_client.delete_vectors = MagicMock()

    with (
        patch.dict(
            "os.environ",
            {
                "S3_ASSISTANTS_VECTOR_STORE_BUCKET_NAME": "test-bucket",
                "S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME": "test-index",
            },
        ),
        patch("boto3.client", return_value=mock_client),
    ):
        import apis.shared.embeddings.bedrock_embeddings as mod

        importlib.reload(mod)
        yield mod, mock_client


@pytest.mark.asyncio
async def test_deterministic_delete_generates_correct_keys(mock_s3vectors):
    """For chunk_count=5, keys should be DOC-123#0 through DOC-123#4."""
    mod, mock_client = mock_s3vectors

    result = await mod.delete_vectors_for_document_deterministic("DOC-123", 5)

    assert result == 5
    mock_client.delete_vectors.assert_called_once()
    call_kwargs = mock_client.delete_vectors.call_args[1]
    assert call_kwargs["vectorBucketName"] == "test-bucket"
    assert call_kwargs["indexName"] == "test-index"
    assert call_kwargs["keys"] == [
        "DOC-123#0",
        "DOC-123#1",
        "DOC-123#2",
        "DOC-123#3",
        "DOC-123#4",
    ]


@pytest.mark.asyncio
async def test_deterministic_delete_batches_at_500(mock_s3vectors):
    """For chunk_count=1200, expect 3 batches: 500, 500, 200."""
    mod, mock_client = mock_s3vectors

    result = await mod.delete_vectors_for_document_deterministic("DOC-456", 1200)

    assert result == 1200
    assert mock_client.delete_vectors.call_count == 3

    batches = [call[1]["keys"] for call in mock_client.delete_vectors.call_args_list]
    assert len(batches[0]) == 500
    assert len(batches[1]) == 500
    assert len(batches[2]) == 200

    # Verify key continuity across batches
    assert batches[0][0] == "DOC-456#0"
    assert batches[0][-1] == "DOC-456#499"
    assert batches[1][0] == "DOC-456#500"
    assert batches[1][-1] == "DOC-456#999"
    assert batches[2][0] == "DOC-456#1000"
    assert batches[2][-1] == "DOC-456#1199"


@pytest.mark.asyncio
async def test_deterministic_delete_zero_chunks(mock_s3vectors):
    """For chunk_count=0, return 0 and make no API calls."""
    mod, mock_client = mock_s3vectors

    result = await mod.delete_vectors_for_document_deterministic("DOC-789", 0)

    assert result == 0
    mock_client.delete_vectors.assert_not_called()


@pytest.mark.asyncio
async def test_deterministic_delete_returns_chunk_count(mock_s3vectors):
    """Return value should always equal chunk_count."""
    mod, _ = mock_s3vectors

    for count in [1, 10, 499, 500, 501, 1000]:
        result = await mod.delete_vectors_for_document_deterministic("DOC-X", count)
        assert result == count


@pytest.mark.asyncio
async def test_deterministic_delete_single_batch(mock_s3vectors):
    """For chunk_count=500, expect exactly 1 batch of 500 keys."""
    mod, mock_client = mock_s3vectors

    result = await mod.delete_vectors_for_document_deterministic("DOC-EXACT", 500)

    assert result == 500
    assert mock_client.delete_vectors.call_count == 1
    keys = mock_client.delete_vectors.call_args[1]["keys"]
    assert len(keys) == 500
    assert keys[0] == "DOC-EXACT#0"
    assert keys[-1] == "DOC-EXACT#499"
