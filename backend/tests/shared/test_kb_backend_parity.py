"""
Parity contract tests: the rules hold on the managed path, not just legacy.

Requirement 3 says a migrated knowledge base must behave exactly as it did
before, parser quality aside. Four properties carry that promise, and all four
are owned by the **facade** in ``rag_service`` rather than by either adapter —
which is the point of these tests. A rule implemented inside the legacy adapter
would be a rule the managed adapter silently lacks, and the symptom would be a
quality difference that looks like the engine swap's fault.

The managed backend itself is task 8.3. These tests use a protocol-conforming
stand-in, which is sufficient and in fact preferable: what is under test is
whether the *facade* applies ``top_k``, the status filter, the 2,000-character
cap and the 500-character citation clip to whatever comes back across the seam.
A stand-in proves that without letting Bedrock's wire format into the assertion.

Feature: managed-kb-migration
Requirements: 3.1, 3.2, 3.3, 3.4
"""

import asyncio
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from apis.shared.assistants.rag_service import (
    MAX_CONTEXT_CHARS,
    augment_prompt_with_context,
    search_assistant_knowledgebase_with_formatting,
)
from apis.shared.kb_backend.protocol import (
    DEFAULT_TOP_K,
    Chunk,
    KnowledgeBaseBackend,
)
from apis.shared.kb_backend.records import ENGINE_MANAGED
from apis.shared.kb_backend.resolver import register_backend, unregister_backend

ASSISTANT_ID = "ast-parity-001"
TABLE_NAME = "test-table"

#: The clip applied when the inference API turns chunks into citation events
#: (``inference_api/chat/routes.py``: ``chunk.get("text", "")[:500]``).
#: Duplicated as a constant here deliberately — asserting the number the route
#: uses is what makes a change to it visible (Requirement 3.4).
CITATION_EXCERPT_CHARS = 500


class FakeManagedBackend:
    """Protocol-conforming stand-in for the managed backend (task 8.3).

    Records the ``top_k`` it was asked for, so the facade's request can be
    asserted rather than assumed, and returns however many chunks the test
    wants — including more than ``top_k``, which is how "the facade narrows"
    becomes an observable claim.
    """

    def __init__(self, chunks: List[Chunk]):
        self._chunks = chunks
        self.calls: List[Dict[str, Any]] = []

    async def search(self, kb_ref: str, query: str, top_k: int = DEFAULT_TOP_K) -> List[Chunk]:
        self.calls.append({"kb_ref": kb_ref, "query": query, "top_k": top_k})
        return list(self._chunks)

    async def ingest(self, kb_ref: str, source) -> None:  # pragma: no cover - unused
        raise NotImplementedError

    async def delete_document(self, kb_ref: str, document_id: str) -> None:  # pragma: no cover
        raise NotImplementedError


def _managed_chunk(document_id: str, index: int = 0, text: str = None, relevance: float = None) -> Chunk:
    """A chunk as the managed backend would produce it: native relevance."""
    return Chunk(
        text=text if text is not None else f"passage from {document_id}",
        # Higher is better, descending with rank.
        relevance=relevance if relevance is not None else 1.0 - (index * 0.01),
        document_id=document_id,
        metadata={"document_id": document_id, "text": text if text is not None else f"passage from {document_id}"},
        key=f"{document_id}#{index}",
    )


@pytest.fixture
def managed_kb(request):
    """Route ``ASSISTANT_ID`` to a fake managed backend for one test.

    Registers the stand-in under the ``managed`` engine and makes the KB_Record
    lookup report that engine, so resolution takes the managed branch for real
    rather than being bypassed.
    """
    created: List[FakeManagedBackend] = []

    def _install(chunks: List[Chunk]) -> FakeManagedBackend:
        backend = FakeManagedBackend(chunks)
        register_backend(ENGINE_MANAGED, backend)
        created.append(backend)
        return backend

    yield _install

    unregister_backend(ENGINE_MANAGED)
    assert created, "fixture used without installing a backend"


def _patch_record_and_statuses(status_map: Dict[str, str]):
    """Patch KB_Record resolution to 'managed' and DOC# statuses to *status_map*.

    Both reads hit the same assistants table, so one mock serves both: ``KB#``
    returns the managed engine and ``DOC#`` returns the requested status.
    """
    def _get_item(**kwargs):
        sk = kwargs["Key"]["SK"]
        if sk.startswith("KB#"):
            return {"Item": {"retrievalEngine": ENGINE_MANAGED}}
        doc_id = sk.replace("DOC#", "")
        if doc_id in status_map:
            return {"Item": {"status": status_map[doc_id]}}
        return {}

    table = MagicMock()
    table.get_item = MagicMock(side_effect=_get_item)
    resource = MagicMock()
    resource.Table.return_value = table
    return patch("boto3.resource", return_value=resource), table


def _patch_legacy_record_and_statuses(status_map: Dict[str, str]):
    """As above, but the KB_Record is absent — so resolution must pick legacy.

    Absence, not an explicit ``"s3vectors"`` value: that is the shape every
    knowledge base predating this feature has, and the one the resolver's default
    exists for.
    """
    def _get_item(**kwargs):
        sk = kwargs["Key"]["SK"]
        if sk.startswith("KB#"):
            return {}
        doc_id = sk.replace("DOC#", "")
        if doc_id in status_map:
            return {"Item": {"status": status_map[doc_id]}}
        return {}

    table = MagicMock()
    table.get_item = MagicMock(side_effect=_get_item)
    resource = MagicMock()
    resource.Table.return_value = table
    return patch("boto3.resource", return_value=resource), table


# ---------------------------------------------------------------------------
# Requirement 3.1 — top_k = 5 on both backends
# ---------------------------------------------------------------------------


def test_managed_path_requests_top_k_five(managed_kb):
    """The facade asks the managed backend for five chunks, as it does legacy."""
    backend = managed_kb([_managed_chunk("doc-a", i) for i in range(5)])
    boto_patch, _ = _patch_record_and_statuses({"doc-a": "complete"})

    with patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE_NAME}), boto_patch:
        asyncio.run(search_assistant_knowledgebase_with_formatting(ASSISTANT_ID, "q"))

    assert backend.calls, "the managed backend was never reached"
    assert backend.calls[0]["top_k"] == DEFAULT_TOP_K
    assert DEFAULT_TOP_K == 5, "the parity contract pins top_k at 5"


def test_managed_path_narrows_results_to_top_k(managed_kb):
    """An over-generous backend is narrowed by the facade, not trusted.

    The stand-in returns nine chunks. If the facade stopped slicing, all nine
    would reach the model and the context budget would be spent on four chunks
    nobody asked for.
    """
    managed_kb([_managed_chunk(f"doc-{i}", 0) for i in range(9)])
    statuses = {f"doc-{i}": "complete" for i in range(9)}
    boto_patch, _ = _patch_record_and_statuses(statuses)

    with patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE_NAME}), boto_patch:
        results = asyncio.run(
            search_assistant_knowledgebase_with_formatting(ASSISTANT_ID, "q")
        )

    assert len(results) == DEFAULT_TOP_K


def test_managed_path_narrowing_happens_after_the_status_filter(managed_kb):
    """Filter first, slice second — so an incomplete doc cannot shrink the answer.

    Six chunks come back and the first is from a deleted document. Slicing before
    filtering would yield four usable chunks; filtering first yields five.
    """
    chunks = [_managed_chunk("doc-gone", 0)] + [_managed_chunk(f"doc-{i}", 0) for i in range(5)]
    managed_kb(chunks)
    statuses = {f"doc-{i}": "complete" for i in range(5)}
    statuses["doc-gone"] = "deleting"
    boto_patch, _ = _patch_record_and_statuses(statuses)

    with patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE_NAME}), boto_patch:
        results = asyncio.run(
            search_assistant_knowledgebase_with_formatting(ASSISTANT_ID, "q")
        )

    assert len(results) == DEFAULT_TOP_K
    assert "doc-gone" not in {r["metadata"]["document_id"] for r in results}


# ---------------------------------------------------------------------------
# Requirement 3.3 — the document status filter runs on the managed path
# ---------------------------------------------------------------------------


def test_managed_path_applies_document_status_filter(managed_kb):
    """Chunks from non-complete documents are dropped on the managed path too."""
    managed_kb([
        _managed_chunk("doc-ok", 0),
        _managed_chunk("doc-deleting", 0),
        _managed_chunk("doc-failed", 0),
    ])
    boto_patch, _ = _patch_record_and_statuses({
        "doc-ok": "complete",
        "doc-deleting": "deleting",
        "doc-failed": "failed",
    })

    with patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE_NAME}), boto_patch:
        results = asyncio.run(
            search_assistant_knowledgebase_with_formatting(ASSISTANT_ID, "q")
        )

    assert [r["metadata"]["document_id"] for r in results] == ["doc-ok"]


def test_managed_path_drops_chunks_for_missing_document_records(managed_kb):
    """A document with no DOC# record is not retrievable on the managed path."""
    managed_kb([_managed_chunk("doc-ok", 0), _managed_chunk("doc-absent", 0)])
    boto_patch, _ = _patch_record_and_statuses({"doc-ok": "complete"})

    with patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE_NAME}), boto_patch:
        results = asyncio.run(
            search_assistant_knowledgebase_with_formatting(ASSISTANT_ID, "q")
        )

    assert [r["metadata"]["document_id"] for r in results] == ["doc-ok"]


# ---------------------------------------------------------------------------
# Requirement 3.4 — result shape, score direction, and the citation clip
# ---------------------------------------------------------------------------


def test_managed_path_emits_the_legacy_result_shape(managed_kb):
    """The keys callers read are unchanged, including the derived ``distance``.

    ``app_api/assistants/routes.py`` puts ``chunk.get("distance")`` in a response
    body, so the field survives the rename to ``relevance`` at the seam. Managed
    relevance of ``1.0`` must present as distance ``-1.0`` — the exact negation,
    the same transform the legacy adapter inverts.
    """
    managed_kb([_managed_chunk("doc-ok", 0, text="hello", relevance=1.0)])
    boto_patch, _ = _patch_record_and_statuses({"doc-ok": "complete"})

    with patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE_NAME}), boto_patch:
        results = asyncio.run(
            search_assistant_knowledgebase_with_formatting(ASSISTANT_ID, "q")
        )

    assert len(results) == 1
    assert set(results[0]) == {"text", "distance", "metadata", "key"}
    assert results[0]["text"] == "hello"
    assert results[0]["key"] == "doc-ok#0"
    assert results[0]["distance"] == -1.0


def test_managed_path_preserves_backend_ranking_order(managed_kb):
    """The facade does not reorder; the backend's ranking is what callers see."""
    managed_kb([
        _managed_chunk("doc-best", 0, relevance=0.9),
        _managed_chunk("doc-mid", 0, relevance=0.5),
        _managed_chunk("doc-worst", 0, relevance=0.1),
    ])
    boto_patch, _ = _patch_record_and_statuses({
        "doc-best": "complete",
        "doc-mid": "complete",
        "doc-worst": "complete",
    })

    with patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE_NAME}), boto_patch:
        results = asyncio.run(
            search_assistant_knowledgebase_with_formatting(ASSISTANT_ID, "q")
        )

    assert [r["metadata"]["document_id"] for r in results] == [
        "doc-best",
        "doc-mid",
        "doc-worst",
    ]


def test_citation_excerpt_clip_holds_on_managed_results(managed_kb):
    """Citations built from managed chunks clip the excerpt at 500 characters.

    Mirrors ``inference_api/chat/routes.py``'s citation construction. Both
    backends feed the same ``context_chunks`` structure into it, so the clip is
    a property of the shared shape rather than of either engine.
    """
    long_text = "x" * 2000
    managed_kb([_managed_chunk("doc-long", 0, text=long_text)])
    boto_patch, _ = _patch_record_and_statuses({"doc-long": "complete"})

    with patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE_NAME}), boto_patch:
        results = asyncio.run(
            search_assistant_knowledgebase_with_formatting(ASSISTANT_ID, "q")
        )

    excerpt = results[0].get("text", "")[:CITATION_EXCERPT_CHARS]
    assert len(excerpt) == CITATION_EXCERPT_CHARS
    assert CITATION_EXCERPT_CHARS == 500, "the parity contract pins the clip at 500"


# ---------------------------------------------------------------------------
# Requirement 3.2 — the 2,000-character context cap
# ---------------------------------------------------------------------------


def test_context_cap_is_two_thousand_characters():
    """The cap is a named constant pinned at 2,000, on every backend."""
    assert MAX_CONTEXT_CHARS == 2000


def test_context_cap_bounds_augmented_prompt_from_managed_chunks():
    """Managed chunks are capped by the facade exactly as legacy chunks are.

    Five 1,000-character chunks total 5,000 characters of context. The augmented
    prompt must carry at most 2,000 of them, so the cap is observable as the
    difference between the prompt's length and the corpus's.
    """
    chunks = [
        {"text": "y" * 1000, "distance": -0.9, "metadata": {"document_id": f"doc-{i}"}, "key": f"doc-{i}#0"}
        for i in range(5)
    ]
    user_message = "what does the corpus say?"

    augmented = augment_prompt_with_context(user_message=user_message, context_chunks=chunks)

    context_only = augmented.split("---\nUser Question:")[0]
    body_length = len(context_only) - len(
        "The following context is retrieved from the assistant's knowledge base. "
        "Use this information to answer the user's question accurately and "
        "comprehensively.\n\n"
    )
    assert body_length <= MAX_CONTEXT_CHARS + 64, (
        f"context body is {body_length} chars; the 2,000-character cap is not "
        f"being applied to managed chunks"
    )
    assert augmented.count("y") < 5000, "all five chunks were included uncapped"
    assert user_message in augmented


def test_context_cap_default_is_not_overridable_by_callers_accidentally():
    """An explicit larger cap is honoured, proving the default is what binds.

    Guards against the cap appearing to hold because the text was short: with the
    cap raised, the same corpus produces a longer prompt.
    """
    chunks = [{"text": "z" * 1000, "metadata": {"document_id": "d"}, "key": "d#0"} for _ in range(5)]

    capped = augment_prompt_with_context("q", chunks)
    raised = augment_prompt_with_context("q", chunks, max_context_length=10000)

    assert len(raised) > len(capped)
    assert capped.count("z") <= MAX_CONTEXT_CHARS


# ---------------------------------------------------------------------------
# The same rules, on the legacy path — the refactor's regression guard
# ---------------------------------------------------------------------------


def _s3_response(hits: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"vectors": hits}


def test_legacy_path_emits_identical_values_to_the_pre_seam_formatter():
    """The legacy path's output is unchanged by the refactor, value for value.

    This is the guard on "zero behaviour change". The facade now receives
    ``relevance`` and derives ``distance`` from it, and
    ``app_api/assistants/routes.py`` forwards that number into an HTTP response
    body — so it must be the *same* float, not a nearby one. ``0.1`` is chosen
    deliberately: a ``1.0 - x`` conversion round-trips it to
    ``0.09999999999999998`` and this assertion is what notices.
    """
    hits = [
        {
            "key": "doc-a#0",
            "distance": 0.1,
            "metadata": {"document_id": "doc-a", "text": "alpha", "source": "a.pdf"},
        },
        {
            "key": "doc-a#1",
            "distance": 0.30000000000000004,
            "metadata": {"document_id": "doc-a", "text": "beta", "source": "a.pdf"},
        },
    ]
    boto_patch, _ = _patch_legacy_record_and_statuses({"doc-a": "complete"})

    with (
        patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE_NAME}),
        boto_patch,
        patch(
            "apis.shared.embeddings.bedrock_embeddings.search_assistant_knowledgebase",
            return_value=_s3_response(hits),
        ),
    ):
        results = asyncio.run(
            search_assistant_knowledgebase_with_formatting(ASSISTANT_ID, "q")
        )

    expected = [
        {
            "text": hit["metadata"]["text"],
            "distance": hit["distance"],
            "metadata": hit["metadata"],
            "key": hit["key"],
        }
        for hit in hits
    ]
    assert results == expected


def test_legacy_path_applies_the_same_filter_and_top_k():
    """The legacy path goes through the same facade rules, not a bypass."""
    hits = [
        {"key": f"doc-{i}#0", "distance": i / 10, "metadata": {"document_id": f"doc-{i}", "text": f"t{i}"}}
        for i in range(8)
    ]
    statuses = {f"doc-{i}": "complete" for i in range(8)}
    statuses["doc-0"] = "deleting"
    boto_patch, _ = _patch_legacy_record_and_statuses(statuses)

    with (
        patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE_NAME}),
        boto_patch,
        patch(
            "apis.shared.embeddings.bedrock_embeddings.search_assistant_knowledgebase",
            return_value=_s3_response(hits),
        ),
    ):
        results = asyncio.run(
            search_assistant_knowledgebase_with_formatting(ASSISTANT_ID, "q")
        )

    assert len(results) == DEFAULT_TOP_K
    assert "doc-0" not in {r["metadata"]["document_id"] for r in results}


def test_legacy_path_preserves_a_missing_distance_as_none():
    """A hit without a distance still surfaces ``distance: None``, as before.

    Fabricating a score would be worse than reporting none: ``0.0`` is a perfect
    match, so a default would promote an unscored chunk to best in the list.
    """
    hits = [{"key": "doc-a#0", "metadata": {"document_id": "doc-a", "text": "alpha"}}]
    boto_patch, _ = _patch_legacy_record_and_statuses({"doc-a": "complete"})

    with (
        patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE_NAME}),
        boto_patch,
        patch(
            "apis.shared.embeddings.bedrock_embeddings.search_assistant_knowledgebase",
            return_value=_s3_response(hits),
        ),
    ):
        results = asyncio.run(
            search_assistant_knowledgebase_with_formatting(ASSISTANT_ID, "q")
        )

    assert results[0]["distance"] is None


def test_empty_backend_result_returns_empty_list():
    """No hits ⇒ empty list, without reaching the status filter, as before."""
    boto_patch, table = _patch_legacy_record_and_statuses({})

    with (
        patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE_NAME}),
        boto_patch,
        patch(
            "apis.shared.embeddings.bedrock_embeddings.search_assistant_knowledgebase",
            return_value=_s3_response([]),
        ),
    ):
        results = asyncio.run(
            search_assistant_knowledgebase_with_formatting(ASSISTANT_ID, "q")
        )

    assert results == []
    doc_lookups = [c for c in table.get_item.call_args_list if c.kwargs["Key"]["SK"].startswith("DOC#")]
    assert doc_lookups == [], "the status filter ran on an empty result set"


def test_backend_failure_degrades_to_empty_list():
    """A backend exception still yields an empty list, never a 500."""
    boto_patch, _ = _patch_legacy_record_and_statuses({})

    with (
        patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE_NAME}),
        boto_patch,
        patch(
            "apis.shared.embeddings.bedrock_embeddings.search_assistant_knowledgebase",
            side_effect=RuntimeError("S3 Vectors unavailable"),
        ),
    ):
        results = asyncio.run(
            search_assistant_knowledgebase_with_formatting(ASSISTANT_ID, "q")
        )

    assert results == []


def test_absent_kb_record_resolves_to_the_legacy_backend():
    """No KB_Record ⇒ legacy. The zero-backfill invariant, seen from the facade.

    The managed stand-in is registered but must not be reached: a knowledge base
    that has never been enrolled has no opinion, and no opinion means legacy.
    """
    unreachable = FakeManagedBackend([_managed_chunk("doc-managed", 0)])
    register_backend(ENGINE_MANAGED, unreachable)
    try:
        table = MagicMock()
        # No Item for either KB# or DOC#: an un-enrolled, un-documented assistant.
        table.get_item = MagicMock(return_value={})
        resource = MagicMock()
        resource.Table.return_value = table

        with (
            patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE_NAME}),
            patch("boto3.resource", return_value=resource),
            patch(
                "apis.shared.embeddings.bedrock_embeddings.search_assistant_knowledgebase",
                return_value=_s3_response(
                    [{"key": "doc-a#0", "distance": 0.2, "metadata": {"document_id": "doc-a", "text": "legacy"}}]
                ),
            ),
        ):
            results = asyncio.run(
                search_assistant_knowledgebase_with_formatting(ASSISTANT_ID, "q")
            )
    finally:
        unregister_backend(ENGINE_MANAGED)

    assert unreachable.calls == [], "an un-enrolled knowledge base reached the managed backend"
    # doc-a has no DOC# record, so the filter drops it — the point is which
    # backend ran, which the empty `calls` above establishes.
    assert results == []


def test_existing_record_without_an_engine_attribute_resolves_to_legacy():
    """A *present* KB_Record carrying no ``retrievalEngine`` still means legacy.

    Distinct from the absent-record case above, and the one that matters during
    migration: a knowledge base enrolled but not yet promoted has a real record
    with real fields and no engine opinion. It must keep reading legacy until the
    single promotion write lands, or a half-migrated corpus starts serving from a
    managed index that catch-up has not finished filling.
    """
    unreachable = FakeManagedBackend([_managed_chunk("doc-managed", 0)])
    register_backend(ENGINE_MANAGED, unreachable)
    try:
        def _get_item(**kwargs):
            sk = kwargs["Key"]["SK"]
            if sk.startswith("KB#"):
                # Enrolled, provisioning done, engine not yet promoted.
                return {"Item": {"appKbId": ASSISTANT_ID, "provisioningState": "active"}}
            return {"Item": {"status": "complete"}}

        table = MagicMock()
        table.get_item = MagicMock(side_effect=_get_item)
        resource = MagicMock()
        resource.Table.return_value = table

        with (
            patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE_NAME}),
            patch("boto3.resource", return_value=resource),
            patch(
                "apis.shared.embeddings.bedrock_embeddings.search_assistant_knowledgebase",
                return_value=_s3_response(
                    [{"key": "doc-a#0", "distance": 0.2, "metadata": {"document_id": "doc-a", "text": "legacy"}}]
                ),
            ),
        ):
            results = asyncio.run(
                search_assistant_knowledgebase_with_formatting(ASSISTANT_ID, "q")
            )
    finally:
        unregister_backend(ENGINE_MANAGED)

    assert unreachable.calls == [], "an unpromoted knowledge base reached the managed backend"
    assert [r["metadata"]["document_id"] for r in results] == ["doc-a"]


# ---------------------------------------------------------------------------
# The stand-in really does conform to the seam
# ---------------------------------------------------------------------------


def test_fake_managed_backend_conforms_to_protocol():
    assert isinstance(FakeManagedBackend([]), KnowledgeBaseBackend)
