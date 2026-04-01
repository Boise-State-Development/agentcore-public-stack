"""
Unit tests for search path document status filtering.

Tests the `_filter_vectors_by_document_status` helper in rag_service.py
which filters vector search results to only include chunks from documents
with status='complete' in DynamoDB.

Feature: reliable-document-deletion
Requirements: 3.1, 3.2, 3.3, 3.4
"""

from unittest.mock import MagicMock, patch



def _make_vector(doc_id, chunk_idx=0):
    """Build a vector result dict matching the shape returned by S3 Vectors."""
    return {
        "key": f"{doc_id}#{chunk_idx}",
        "distance": 0.5,
        "metadata": {"document_id": doc_id, "text": f"chunk from {doc_id}"},
    }


ASSISTANT_ID = "ast-test-001"
TABLE_NAME = "test-table"
ENV_PATCH = {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE_NAME}


def _build_mock_table(status_map):
    """Return a mock DynamoDB table whose get_item returns statuses from *status_map*.

    Keys present in *status_map* return {"Item": {"status": value}}.
    Keys absent simulate a missing record (no "Item" key).
    """
    mock_table = MagicMock()

    def _get_item(**kwargs):
        doc_id = kwargs["Key"]["SK"].replace("DOC#", "")
        if doc_id in status_map:
            return {"Item": {"status": status_map[doc_id]}}
        return {}  # no Item → record not found

    mock_table.get_item = MagicMock(side_effect=_get_item)
    return mock_table


def _setup_dynamo_mock(mock_boto3_resource, status_map):
    """Wire a mock boto3.resource('dynamodb') to return a table with *status_map*."""
    mock_table = _build_mock_table(status_map)
    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_table
    mock_boto3_resource.return_value = mock_dynamo
    return mock_table


# -----------------------------------------------------------------------
# Requirement 3.1, 3.2: Only chunks from complete documents are returned
# -----------------------------------------------------------------------


@patch("boto3.resource")
@patch.dict("os.environ", ENV_PATCH)
def test_filter_keeps_only_complete_documents(mock_boto3_resource):
    """Mix of complete and deleting docs — only complete chunks returned."""
    _setup_dynamo_mock(mock_boto3_resource, {
        "doc-a": "complete",
        "doc-b": "deleting",
        "doc-c": "complete",
    })

    from apis.shared.assistants.rag_service import _filter_vectors_by_document_status

    vectors = [
        _make_vector("doc-a", 0),
        _make_vector("doc-b", 0),
        _make_vector("doc-b", 1),
        _make_vector("doc-c", 0),
    ]

    result = _filter_vectors_by_document_status(vectors, ASSISTANT_ID)

    doc_ids = [v["metadata"]["document_id"] for v in result]
    assert doc_ids == ["doc-a", "doc-c"]


# -----------------------------------------------------------------------
# Requirement 3.2: All deleting → empty result
# -----------------------------------------------------------------------


@patch("boto3.resource")
@patch.dict("os.environ", ENV_PATCH)
def test_filter_excludes_all_deleting(mock_boto3_resource):
    """All documents in 'deleting' status — result should be empty."""
    _setup_dynamo_mock(mock_boto3_resource, {
        "doc-x": "deleting",
        "doc-y": "deleting",
    })

    from apis.shared.assistants.rag_service import _filter_vectors_by_document_status

    vectors = [_make_vector("doc-x"), _make_vector("doc-y")]

    result = _filter_vectors_by_document_status(vectors, ASSISTANT_ID)

    assert result == []


# -----------------------------------------------------------------------
# Requirement 3.2: All complete → all chunks returned
# -----------------------------------------------------------------------


@patch("boto3.resource")
@patch.dict("os.environ", ENV_PATCH)
def test_filter_keeps_all_complete(mock_boto3_resource):
    """All documents in 'complete' status — all chunks returned."""
    _setup_dynamo_mock(mock_boto3_resource, {
        "doc-1": "complete",
        "doc-2": "complete",
    })

    from apis.shared.assistants.rag_service import _filter_vectors_by_document_status

    vectors = [
        _make_vector("doc-1", 0),
        _make_vector("doc-1", 1),
        _make_vector("doc-2", 0),
    ]

    result = _filter_vectors_by_document_status(vectors, ASSISTANT_ID)

    assert len(result) == 3
    doc_ids = [v["metadata"]["document_id"] for v in result]
    assert doc_ids == ["doc-1", "doc-1", "doc-2"]


# -----------------------------------------------------------------------
# Requirement 3.3: Missing DynamoDB record → excluded
# -----------------------------------------------------------------------


@patch("boto3.resource")
@patch.dict("os.environ", ENV_PATCH)
def test_filter_excludes_missing_records(mock_boto3_resource):
    """Document not found in DynamoDB (no Item) — chunks excluded."""
    _setup_dynamo_mock(mock_boto3_resource, {
        "doc-exists": "complete",
        # "doc-missing" intentionally absent
    })

    from apis.shared.assistants.rag_service import _filter_vectors_by_document_status

    vectors = [
        _make_vector("doc-exists", 0),
        _make_vector("doc-missing", 0),
    ]

    result = _filter_vectors_by_document_status(vectors, ASSISTANT_ID)

    doc_ids = [v["metadata"]["document_id"] for v in result]
    assert doc_ids == ["doc-exists"]


# -----------------------------------------------------------------------
# Requirement 3.4: DynamoDB error → graceful degradation (unfiltered)
# -----------------------------------------------------------------------


@patch("boto3.resource")
@patch.dict("os.environ", ENV_PATCH)
def test_filter_graceful_degradation_on_dynamo_error(mock_boto3_resource):
    """DynamoDB raises exception — return unfiltered results."""
    mock_dynamo = MagicMock()
    mock_dynamo.Table.side_effect = Exception("DynamoDB unavailable")
    mock_boto3_resource.return_value = mock_dynamo

    from apis.shared.assistants.rag_service import _filter_vectors_by_document_status

    vectors = [
        _make_vector("doc-a", 0),
        _make_vector("doc-b", 0),
    ]

    result = _filter_vectors_by_document_status(vectors, ASSISTANT_ID)

    # Graceful degradation: all vectors returned unfiltered
    assert len(result) == 2
    doc_ids = [v["metadata"]["document_id"] for v in result]
    assert doc_ids == ["doc-a", "doc-b"]


# -----------------------------------------------------------------------
# Edge case: Empty vector list
# -----------------------------------------------------------------------


@patch("boto3.resource")
@patch.dict("os.environ", ENV_PATCH)
def test_filter_empty_vectors(mock_boto3_resource):
    """Empty vector list — returns empty without calling DynamoDB."""
    from apis.shared.assistants.rag_service import _filter_vectors_by_document_status

    result = _filter_vectors_by_document_status([], ASSISTANT_ID)

    assert result == []
    # boto3.resource should not be called for empty input
    mock_boto3_resource.assert_not_called()
