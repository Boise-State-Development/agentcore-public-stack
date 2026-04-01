"""
Property-based tests for search path document status filtering.

Feature: reliable-document-deletion
"""

from unittest.mock import MagicMock, patch

from hypothesis import given, settings, strategies as st

# ---------------------------------------------------------------------------
# Shared Hypothesis strategies
# ---------------------------------------------------------------------------

# Document statuses that can exist in DynamoDB (or None for missing record)
st_document_status = st.sampled_from(
    ["complete", "deleting", "failed", "uploading", "chunking", "embedding", None]
)

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

# Strategy for a list of (document_id, status_or_none) pairs with unique doc IDs
st_doc_status_pairs = st.lists(
    st.tuples(st_document_id, st_document_status),
    min_size=1,
    max_size=15,
    unique_by=lambda pair: pair[0],
)


# ---------------------------------------------------------------------------
# Property 3: Search results only contain complete documents
# ---------------------------------------------------------------------------


@given(
    assistant_id=st_assistant_id,
    doc_status_pairs=st_doc_status_pairs,
)
@settings(max_examples=100, deadline=None)
def test_search_results_only_contain_complete_documents(
    assistant_id, doc_status_pairs
):
    """
    **Validates: Requirements 3.1, 3.2, 3.3**

    For any mix of document statuses (complete, deleting, failed, uploading,
    chunking, embedding, or None for missing), verify that
    _filter_vectors_by_document_status returns only chunks from documents
    with status="complete".
    """
    # Build vector results referencing these documents
    vectors = []
    for doc_id, _ in doc_status_pairs:
        vectors.append({
            "key": f"{doc_id}#0",
            "distance": 0.5,
            "metadata": {
                "document_id": doc_id,
                "text": f"chunk from {doc_id}",
            },
        })

    # Build a mock DynamoDB table that returns the appropriate status per doc
    status_map = {doc_id: status for doc_id, status in doc_status_pairs}

    def mock_get_item(**kwargs):
        key = kwargs["Key"]
        doc_id = key["SK"].replace("DOC#", "")
        status = status_map.get(doc_id)
        if status is None:
            # Missing document — no Item in response
            return {}
        return {"Item": {"status": status}}

    mock_table = MagicMock()
    mock_table.get_item = MagicMock(side_effect=mock_get_item)

    mock_dynamodb = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    with (
        patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": "test-table"}),
        patch("boto3.resource", return_value=mock_dynamodb),
    ):
        from apis.shared.assistants.rag_service import (
            _filter_vectors_by_document_status,
        )

        filtered = _filter_vectors_by_document_status(vectors, assistant_id)

    # Determine which doc IDs should be in the results
    expected_doc_ids = {
        doc_id for doc_id, status in doc_status_pairs if status == "complete"
    }
    excluded_doc_ids = {
        doc_id for doc_id, status in doc_status_pairs if status != "complete"
    }

    # All returned chunks must be from "complete" documents
    for v in filtered:
        returned_doc_id = v["metadata"]["document_id"]
        assert returned_doc_id in expected_doc_ids, (
            f"Chunk from doc '{returned_doc_id}' should not be in results "
            f"(status={status_map.get(returned_doc_id)})"
        )

    # All "complete" document chunks must be present
    returned_doc_ids = {v["metadata"]["document_id"] for v in filtered}
    for doc_id in expected_doc_ids:
        assert doc_id in returned_doc_ids, (
            f"Chunk from complete doc '{doc_id}' is missing from results"
        )

    # No excluded document chunks should be present
    for doc_id in excluded_doc_ids:
        assert doc_id not in returned_doc_ids, (
            f"Chunk from non-complete doc '{doc_id}' should be excluded "
            f"(status={status_map.get(doc_id)})"
        )
