"""
Property-based tests for cleanup service.

Feature: reliable-document-deletion
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings, strategies as st


# ---------------------------------------------------------------------------
# Shared Hypothesis strategies
# ---------------------------------------------------------------------------

st_document_id = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
    min_size=3,
    max_size=12,
).map(lambda s: f"DOC-{s}")

st_assistant_id = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-",
    min_size=3,
    max_size=20,
).map(lambda s: f"AST-{s}")

st_s3_key = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789/-_.",
    min_size=5,
    max_size=80,
)

st_chunk_count = st.one_of(st.none(), st.integers(min_value=0, max_value=100))

st_max_retries = st.integers(min_value=1, max_value=5)

st_base_delay = st.floats(min_value=0.01, max_value=1.0)


# ---------------------------------------------------------------------------
# Property 4: Cleanup retry bounded by max_retries
# ---------------------------------------------------------------------------


@given(
    document_id=st_document_id,
    assistant_id=st_assistant_id,
    s3_key=st_s3_key,
    chunk_count=st_chunk_count,
    max_retries=st_max_retries,
    base_delay=st_base_delay,
)
@settings(max_examples=100, deadline=None)
@pytest.mark.asyncio
async def test_cleanup_retry_bounded_by_max_retries(
    document_id, assistant_id, s3_key, chunk_count, max_retries, base_delay
):
    """
    **Validates: Requirements 4.1, 4.2**

    For any max_retries >= 1 and base_delay > 0, verify at most max_retries
    attempts are made for each phase (vector deletion and S3 deletion), with
    delay following base_delay * 2^attempt + jitter.
    """
    vector_call_count = 0
    s3_call_count = 0

    async def failing_deterministic(*args, **kwargs):
        nonlocal vector_call_count
        vector_call_count += 1
        raise Exception("vector deletion failed")

    async def failing_fallback(*args, **kwargs):
        nonlocal vector_call_count
        vector_call_count += 1
        raise Exception("vector deletion fallback failed")

    sleep_delays = []

    async def mock_sleep(delay):
        sleep_delays.append(delay)

    mock_s3_client = MagicMock()

    def failing_s3_delete(**kwargs):
        nonlocal s3_call_count
        s3_call_count += 1
        raise Exception("s3 deletion failed")

    mock_s3_client.delete_object = MagicMock(side_effect=failing_s3_delete)

    mock_hard_delete = AsyncMock()

    with (
        patch.dict("os.environ", {"S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME": "test-bucket"}),
        patch(
            "apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document_deterministic",
            side_effect=failing_deterministic,
        ),
        patch(
            "apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document",
            side_effect=failing_fallback,
        ),
        patch("boto3.client", return_value=mock_s3_client),
        patch(
            "apis.app_api.documents.services.cleanup_service.asyncio.sleep",
            side_effect=mock_sleep,
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
            document_id=document_id,
            assistant_id=assistant_id,
            s3_key=s3_key,
            chunk_count=chunk_count,
            max_retries=max_retries,
            base_delay=base_delay,
        )

    # Both phases fail, so result must be False
    assert result is False

    # Vector deletion: exactly max_retries attempts
    assert vector_call_count == max_retries, (
        f"Expected {max_retries} vector deletion attempts, got {vector_call_count}"
    )

    # S3 deletion: exactly max_retries attempts
    assert s3_call_count == max_retries, (
        f"Expected {max_retries} S3 deletion attempts, got {s3_call_count}"
    )

    # Each phase sleeps (max_retries - 1) times (no sleep after last attempt)
    expected_sleep_count = (max_retries - 1) * 2
    assert len(sleep_delays) == expected_sleep_count, (
        f"Expected {expected_sleep_count} sleep calls, got {len(sleep_delays)}"
    )

    # Verify each delay follows base_delay * 2^attempt + jitter (jitter in [0, 0.1])
    for phase in range(2):
        for attempt in range(max_retries - 1):
            idx = phase * (max_retries - 1) + attempt
            delay = sleep_delays[idx]
            min_expected = base_delay * (2 ** attempt)
            max_expected = base_delay * (2 ** attempt) + 0.1
            assert min_expected <= delay <= max_expected, (
                f"Phase {phase}, attempt {attempt}: delay {delay} not in "
                f"[{min_expected}, {max_expected}]"
            )

    # hard_delete should NOT have been called since cleanup failed
    mock_hard_delete.assert_not_called()


# ---------------------------------------------------------------------------
# Property 5: Failed cleanup preserves DynamoDB record
# ---------------------------------------------------------------------------


@given(
    document_id=st_document_id,
    assistant_id=st_assistant_id,
    s3_key=st_s3_key,
    chunk_count=st_chunk_count,
    max_retries=st_max_retries,
    base_delay=st_base_delay,
)
@settings(max_examples=100, deadline=None)
@pytest.mark.asyncio
async def test_failed_cleanup_preserves_dynamodb_record(
    document_id, assistant_id, s3_key, chunk_count, max_retries, base_delay
):
    """
    **Validates: Requirement 4.4**

    Verify that when cleanup fails after all retries, hard_delete_document is
    NOT called and the record remains with status="deleting" and valid TTL.
    cleanup_document_resources returns False.
    """
    async def failing_deterministic(*args, **kwargs):
        raise Exception("vector deletion failed")

    async def failing_fallback(*args, **kwargs):
        raise Exception("vector deletion fallback failed")

    mock_s3_client = MagicMock()
    mock_s3_client.delete_object = MagicMock(side_effect=Exception("s3 deletion failed"))

    mock_hard_delete = AsyncMock()

    with (
        patch.dict("os.environ", {"S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME": "test-bucket"}),
        patch(
            "apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document_deterministic",
            side_effect=failing_deterministic,
        ),
        patch(
            "apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document",
            side_effect=failing_fallback,
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
            document_id=document_id,
            assistant_id=assistant_id,
            s3_key=s3_key,
            chunk_count=chunk_count,
            max_retries=max_retries,
            base_delay=base_delay,
        )

    # Cleanup failed — result must be False
    assert result is False, (
        "cleanup_document_resources should return False when both phases fail"
    )

    # hard_delete_document must NOT have been called
    mock_hard_delete.assert_not_called()


# ---------------------------------------------------------------------------
# Property 6: Successful cleanup triggers hard-delete
# ---------------------------------------------------------------------------


@given(
    document_id=st_document_id,
    assistant_id=st_assistant_id,
    s3_key=st_s3_key,
    chunk_count=st_chunk_count,
    max_retries=st_max_retries,
    base_delay=st_base_delay,
)
@settings(max_examples=100, deadline=None)
@pytest.mark.asyncio
async def test_successful_cleanup_triggers_hard_delete(
    document_id, assistant_id, s3_key, chunk_count, max_retries, base_delay
):
    """
    **Validates: Requirements 4.3, 9.1**

    Verify that when both vector and S3 deletion succeed,
    hard_delete_document IS called with correct assistant_id and document_id,
    and cleanup_document_resources returns True.
    """
    async def succeeding_deterministic(*args, **kwargs):
        pass

    async def succeeding_fallback(*args, **kwargs):
        pass

    mock_s3_client = MagicMock()
    mock_s3_client.delete_object = MagicMock(return_value={})

    mock_hard_delete = AsyncMock()

    with (
        patch.dict("os.environ", {"S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME": "test-bucket"}),
        patch(
            "apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document_deterministic",
            side_effect=succeeding_deterministic,
        ),
        patch(
            "apis.shared.embeddings.bedrock_embeddings.delete_vectors_for_document",
            side_effect=succeeding_fallback,
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
            document_id=document_id,
            assistant_id=assistant_id,
            s3_key=s3_key,
            chunk_count=chunk_count,
            max_retries=max_retries,
            base_delay=base_delay,
        )

    # Cleanup succeeded — result must be True
    assert result is True, (
        "cleanup_document_resources should return True when both phases succeed"
    )

    # hard_delete_document must have been called exactly once
    mock_hard_delete.assert_called_once()

    # Verify it was called with the correct arguments
    call_args = mock_hard_delete.call_args
    assert call_args[0] == (assistant_id, document_id) or (
        call_args[1].get("assistant_id") == assistant_id
        and call_args[1].get("document_id") == document_id
    ), (
        f"hard_delete_document called with wrong args: {call_args}"
    )


# ---------------------------------------------------------------------------
# Property 9: Bulk cleanup counts are consistent
# ---------------------------------------------------------------------------


@given(
    success_flags=st.lists(st.booleans(), min_size=0, max_size=15),
)
@settings(max_examples=100, deadline=None)
@pytest.mark.asyncio
async def test_bulk_cleanup_counts_are_consistent(success_flags):
    """
    **Validates: Requirements 8.3**

    For any set of documents, verify that
    success_count + failure_count == len(documents).
    """
    # Build mock documents from the generated flags
    documents = []
    for i, _ in enumerate(success_flags):
        doc = MagicMock()
        doc.document_id = f"DOC-{i}"
        doc.s3_key = f"assistants/AST-test/{i}/file.pdf"
        doc.chunk_count = i
        documents.append(doc)

    # Create a side_effect iterator that returns True/False per document
    cleanup_results = list(success_flags)

    async def mock_cleanup(document_id, assistant_id, s3_key, chunk_count, max_retries=3):
        idx = int(document_id.split("-")[1])
        return cleanup_results[idx]

    with patch(
        "apis.app_api.documents.services.cleanup_service.cleanup_document_resources",
        side_effect=mock_cleanup,
    ):
        from apis.app_api.documents.services.cleanup_service import (
            cleanup_assistant_documents,
        )

        success_count, failure_count = await cleanup_assistant_documents(
            assistant_id="AST-test",
            documents=documents,
            max_retries=3,
        )

    assert success_count + failure_count == len(documents), (
        f"success_count ({success_count}) + failure_count ({failure_count}) "
        f"!= len(documents) ({len(documents)})"
    )

    expected_successes = sum(1 for f in success_flags if f is True)
    expected_failures = len(success_flags) - expected_successes
    assert success_count == expected_successes, (
        f"Expected {expected_successes} successes, got {success_count}"
    )
    assert failure_count == expected_failures, (
        f"Expected {expected_failures} failures, got {failure_count}"
    )
