"""
Property-based tests for deterministic vector deletion.

Feature: reliable-document-deletion
"""

from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings, strategies as st


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

st_document_id = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    min_size=1,
    max_size=30,
)

st_chunk_count = st.integers(min_value=0, max_value=2000)


# ---------------------------------------------------------------------------
# Property 7: Deterministic vector key generation
# ---------------------------------------------------------------------------


@given(document_id=st_document_id, chunk_count=st_chunk_count)
@settings(max_examples=100)
@pytest.mark.asyncio
async def test_deterministic_vector_key_generation(document_id, chunk_count):
    """
    **Validates: Requirements 5.1**

    For any document_id (text) and chunk_count >= 0, verify:
    - Exactly chunk_count keys are generated matching {document_id}#{i}
    - Keys are batched into groups of at most 500
    - For chunk_count=0, no API calls are made
    - The return value equals chunk_count
    """
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
        # Re-import to pick up patched env vars
        import importlib
        import apis.shared.embeddings.bedrock_embeddings as mod

        importlib.reload(mod)

        result = await mod.delete_vectors_for_document_deterministic(
            document_id, chunk_count
        )

    # Return value equals chunk_count
    assert result == chunk_count

    if chunk_count == 0:
        # No API calls for zero chunks
        mock_client.delete_vectors.assert_not_called()
        return

    # Collect all keys across all delete_vectors calls
    all_keys = []
    for call in mock_client.delete_vectors.call_args_list:
        batch_keys = call[1]["keys"]
        # No batch exceeds 500 keys
        assert len(batch_keys) <= 500, (
            f"Batch size {len(batch_keys)} exceeds limit of 500"
        )
        all_keys.extend(batch_keys)

    # Total keys equals chunk_count
    assert len(all_keys) == chunk_count, (
        f"Expected {chunk_count} keys, got {len(all_keys)}"
    )

    # Each key matches {document_id}#{i} for sequential i
    for i in range(chunk_count):
        expected_key = f"{document_id}#{i}"
        assert all_keys[i] == expected_key, (
            f"Key at index {i}: expected '{expected_key}', got '{all_keys[i]}'"
        )
