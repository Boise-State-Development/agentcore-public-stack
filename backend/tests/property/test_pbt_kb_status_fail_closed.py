"""Property-based tests for fail-closed document status filtering.

Feature: managed-kb-migration

**Property 4: unconfirmable status never leaks.**

The filter's job is to keep chunks belonging to deleted or half-deleted documents
out of retrieval results. Its old fallback returned everything unfiltered whenever
it could not reach DynamoDB, which meant the guard vanished at exactly the moment
it was most likely to matter — and vanished *silently*, since the response looks
identical either way.

This inverts that (Requirement 5, superseding `reliable-document-deletion`
Requirement 3.4). The property asserted here is deliberately absolute: no matter
how many chunks, how many distinct documents, or what shape of table-level failure
is injected, the result is empty. There is no "mostly" — a single leaked chunk from
a deleted document is the entire failure mode.

The per-document lookup failure is a different case and is *not* covered by this
property: that one already skipped only its own document, which is correct, and is
left unchanged.

Validates: Requirements 5.1, 5.2, 24.6.
"""

from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

# Imported at module scope, deliberately, and NOT inside the patched context of a
# test. Importing it lazily made the first-ever run differ from every later one:
# the import itself happened while `boto3.resource` was mocked, so module-level
# import work was performed against a mock exactly once and was then cached in
# sys.modules for the rest of the session. That produced a test that failed on a
# cold run and passed on every warm one — the worst failure mode a guard can have,
# because CI is cold and local re-runs are warm.
from apis.shared.assistants.rag_service import _filter_vectors_by_document_status

ASSISTANT_ID = "ast-failclosed"

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

st_document_id = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=16
).map(lambda s: f"doc-{s}")

#: A non-empty set of vectors spread over an arbitrary number of documents. Both
#: axes matter: the filter dedupes document ids before lookup, so "many chunks,
#: one document" and "one chunk each, many documents" exercise different paths.
st_vectors = st.lists(st_document_id, min_size=1, max_size=12).map(
    lambda ids: [
        {
            "key": f"vec-{i}",
            "distance": 0.1,
            "metadata": {"document_id": d, "text": f"chunk {i}", "assistant_id": ASSISTANT_ID},
        }
        for i, d in enumerate(ids)
    ]
)

#: Table-level failures. Any exception type, raised from the resource or the
#: table handle — the guard must not depend on recognising a specific error.
st_failure = st.sampled_from(
    [
        Exception("DynamoDB unavailable"),
        RuntimeError("connection reset"),
        ValueError("malformed region"),
        KeyError("credentials"),
        TimeoutError("timed out"),
    ]
)


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------
@given(vectors=st_vectors, failure=st_failure)
@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_a_table_level_failure_never_leaks_a_chunk(vectors, failure):
    """Any table-level failure, any corpus shape → zero chunks."""
    with patch("apis.shared.kb_backend.metrics.emit_count"), patch(
        "apis.shared.assistants.rag_service.emit_count"
    ), patch("boto3.resource") as resource, patch.dict(
        "os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": "t", "AWS_REGION": "us-west-2"}
    ):
        dynamo = MagicMock()
        dynamo.Table.side_effect = failure
        resource.return_value = dynamo

        assert _filter_vectors_by_document_status(vectors, ASSISTANT_ID) == []


@given(vectors=st_vectors)
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_a_missing_table_name_never_leaks_a_chunk(vectors):
    """The other former fail-open path: no table configured → zero chunks."""
    with patch("apis.shared.kb_backend.metrics.emit_count"), patch(
        "apis.shared.assistants.rag_service.emit_count"
    ), patch("boto3.resource") as resource, patch.dict("os.environ", {}, clear=True):
        assert _filter_vectors_by_document_status(vectors, ASSISTANT_ID) == []
        # Never contacted, so this is a guard rather than a failed call.
        resource.assert_not_called()


@given(vectors=st_vectors, failure=st_failure)
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_the_degradation_is_always_reported(vectors, failure):
    """An empty result from this path must be distinguishable from an empty corpus.

    Without the signal, a total retrieval outage looks exactly like "nobody's
    documents matched", which is the kind of failure that survives for weeks.
    """
    with patch("apis.shared.assistants.rag_service.emit_count") as emit, patch(
        "boto3.resource"
    ) as resource, patch.dict(
        "os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": "t", "AWS_REGION": "us-west-2"}
    ):
        dynamo = MagicMock()
        dynamo.Table.side_effect = failure
        resource.return_value = dynamo

        _filter_vectors_by_document_status(vectors, ASSISTANT_ID)
        emit.assert_called_once()


# ---------------------------------------------------------------------------
# What must NOT change
# ---------------------------------------------------------------------------
@given(vectors=st_vectors)
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_a_per_document_failure_still_only_drops_that_document(vectors):
    """The inner handler was already correct and is deliberately untouched.

    Inverting the table-level fallback must not be over-applied: one unreadable
    document should cost that document, not the whole result. Here every lookup
    fails individually, so everything drops — but via the per-document path, which
    must NOT report a table-level degradation.
    """
    with patch("apis.shared.assistants.rag_service.emit_count") as emit, patch(
        "boto3.resource"
    ) as resource, patch.dict(
        "os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": "t", "AWS_REGION": "us-west-2"}
    ):
        table = MagicMock()
        table.get_item.side_effect = Exception("per-item failure")
        dynamo = MagicMock()
        dynamo.Table.return_value = table
        resource.return_value = dynamo

        assert _filter_vectors_by_document_status(vectors, ASSISTANT_ID) == []
        emit.assert_not_called()


@given(vectors=st_vectors)
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_complete_documents_are_still_returned(vectors):
    """The happy path, so the properties above cannot pass by always returning []."""
    with patch("apis.shared.assistants.rag_service.emit_count"), patch(
        "boto3.resource"
    ) as resource, patch.dict(
        "os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": "t", "AWS_REGION": "us-west-2"}
    ):
        table = MagicMock()
        table.get_item.return_value = {"Item": {"status": "complete"}}
        dynamo = MagicMock()
        dynamo.Table.return_value = table
        resource.return_value = dynamo

        assert len(_filter_vectors_by_document_status(vectors, ASSISTANT_ID)) == len(vectors)


@pytest.mark.parametrize("status", ["deleting", "failed", "uploading", "chunking"])
def test_a_non_complete_status_is_excluded(status):
    """Unchanged behaviour, pinned: only `complete` is served.

    Production carried 200 of 1,692 document records in a non-complete state
    (101 deleting, 95 failed, 4 uploading), so this is the common case, not an edge.
    """
    with patch("apis.shared.assistants.rag_service.emit_count"), patch(
        "boto3.resource"
    ) as resource, patch.dict(
        "os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": "t", "AWS_REGION": "us-west-2"}
    ):
        table = MagicMock()
        table.get_item.return_value = {"Item": {"status": status}}
        dynamo = MagicMock()
        dynamo.Table.return_value = table
        resource.return_value = dynamo

        vectors = [
            {"key": "v1", "distance": 0.1, "metadata": {"document_id": "doc-a", "text": "t"}}
        ]
        assert _filter_vectors_by_document_status(vectors, ASSISTANT_ID) == []
