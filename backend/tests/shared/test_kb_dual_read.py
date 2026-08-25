"""
Dual-read pilot: legacy serves, managed is observed, nobody waits.

Requirement 18. The pilot's value depends entirely on it being safe to leave
switched on, so the tests here are mostly about what it must *not* do.

The load-bearing one is latency (18.5). Managed ``Retrieve`` measured a
662–695 ms p50 against legacy's 257 ms, so a pilot that awaited both would nearly
triple the retrieval leg of every piloted turn — and it would do so while
producing correct results and passing every other test in this file. It is caught
here by making the managed backend sleep far longer than any test tolerance and
asserting that the facade still returns promptly, which fails if the two calls
are ever gathered instead of detached.

Feature: managed-kb-migration
Requirements: 18.1, 18.2, 18.3, 18.4, 18.5
"""

import asyncio
import time
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from apis.shared.assistants.kb_access import granted
from apis.shared.assistants.rag_service import search_assistant_knowledgebase_with_formatting
from apis.shared.kb_backend.dual_read import (
    DUAL_READ_ATTR,
    compare,
    is_pilot_enabled,
    start_managed_read,
)
from apis.shared.kb_backend.protocol import DEFAULT_TOP_K, Chunk
from apis.shared.kb_backend.records import ENGINE_MANAGED
from apis.shared.kb_backend.resolver import register_backend, unregister_backend

ASSISTANT_ID = "ast-dualread-001"
TABLE_NAME = "test-table"
ACCESS = granted(ASSISTANT_ID, "user-dualread", "owner")

#: Longer than any assertion below tolerates. If the facade ever awaits the
#: managed read, the latency test fails on the clock rather than on a value.
MANAGED_DELAY_SECONDS = 3.0


class StubBackend:
    """Records its calls; optionally sleeps, or raises, or returns nothing."""

    def __init__(
        self,
        chunks: List[Chunk] = None,
        delay: float = 0.0,
        error: Exception = None,
    ):
        self._chunks = chunks or []
        self._delay = delay
        self._error = error
        self.calls: List[Dict[str, Any]] = []

    async def search(self, kb_ref: str, query: str, top_k: int = DEFAULT_TOP_K) -> List[Chunk]:
        self.calls.append({"kb_ref": kb_ref, "query": query, "top_k": top_k})
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return list(self._chunks)

    async def ingest(self, kb_ref: str, source) -> None:  # pragma: no cover - unused
        raise NotImplementedError

    async def delete_document(self, kb_ref: str, document_id: str) -> None:  # pragma: no cover
        raise NotImplementedError


def _chunk(document_id: str, index: int = 0) -> Chunk:
    return Chunk(
        text=f"passage from {document_id}",
        relevance=1.0 - index * 0.01,
        document_id=document_id,
        metadata={"document_id": document_id},
        key=f"{document_id}#{index}",
    )


def _all_documents_complete() -> MagicMock:
    table = MagicMock()
    table.get_item.return_value = {"Item": {"status": "complete"}}
    resource = MagicMock()
    resource.Table.return_value = table
    return resource


@pytest.fixture
def managed_registered():
    """Register a managed backend for the duration of one test.

    Task 14 registers it for real. Registering it here is what lets the pilot be
    tested before the surface that enables it exists — and unregistering
    afterwards is what keeps every other test seeing the shipped state, where a
    dual read is impossible because there is no second backend.
    """
    created: List[StubBackend] = []

    def _register(backend: StubBackend) -> StubBackend:
        register_backend(ENGINE_MANAGED, backend)
        created.append(backend)
        return backend

    yield _register
    unregister_backend(ENGINE_MANAGED)


async def _search(legacy: StubBackend, record: Dict[str, Any], top_k: int = 5):
    """Run the facade with ``record`` as the knowledge base's KB_Record."""
    with patch.dict(
        "os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE_NAME}, clear=False
    ), patch(
        "apis.shared.assistants.rag_service.load_record", return_value=record
    ), patch(
        "apis.shared.assistants.rag_service.resolve_backend", return_value=legacy
    ), patch(
        "apis.shared.assistants.rag_service.boto3.resource",
        return_value=_all_documents_complete(),
    ), patch(
        "apis.shared.assistants.rag_service.emit_count"
    ), patch(
        "apis.shared.kb_backend.dual_read.emit_value"
    ) as emit_value, patch(
        "apis.shared.kb_backend.dual_read.emit_count"
    ) as emit_count:
        results = await search_assistant_knowledgebase_with_formatting(
            ASSISTANT_ID, "q", top_k, access=ACCESS
        )
        # Let the detached comparison run to completion before asserting on it.
        # Not a sleep in the facade — a sleep in the *test*, which is the whole
        # point: the caller never waits, the assertion does.
        for _ in range(200):
            await asyncio.sleep(0)
            if emit_value.called or emit_count.called:
                break
    return results, emit_value, emit_count


# ── Opt-in, default off ──────────────────────────────────────────────────────
class TestThePilotIsOptIn:
    def test_absence_means_off(self):
        """Requirement 18.4. Same convention as ``retrievalEngine``: the default
        costs nothing to express and nothing to revert."""
        assert is_pilot_enabled(None) is False
        assert is_pilot_enabled({}) is False
        assert is_pilot_enabled({"appKbId": "x"}) is False

    @pytest.mark.parametrize("value", ["true", "True", 1, "yes", [1]])
    def test_only_a_real_boolean_true_enrols(self, value):
        """A truthy value left by a hand-edited record must not enrol a knowledge
        base into paying for a second retrieval on every turn. This is the shape
        of the reconciler-arming defect: a permissive read of a flag turned a
        report-only job into a deleting one."""
        assert is_pilot_enabled({DUAL_READ_ATTR: value}) is False

    def test_boolean_true_enrols(self):
        assert is_pilot_enabled({DUAL_READ_ATTR: True}) is True

    @pytest.mark.asyncio
    async def test_no_managed_call_when_not_enrolled(self, managed_registered):
        managed = managed_registered(StubBackend([_chunk("doc-m")]))
        legacy = StubBackend([_chunk("doc-a")])

        results, _, _ = await _search(legacy, {})

        assert len(results) == 1
        assert managed.calls == [], "an unenrolled knowledge base was dual-read"

    @pytest.mark.asyncio
    async def test_no_managed_call_when_no_managed_backend_is_registered(self):
        """The shipped state. A record can carry the flag with no effect, which is
        what makes enabling it in the database harmless before task 14."""
        legacy = StubBackend([_chunk("doc-a")])
        results, emit_value, _ = await _search(legacy, {DUAL_READ_ATTR: True})

        assert len(results) == 1
        assert emit_value.called is False

    @pytest.mark.asyncio
    async def test_no_dual_read_for_an_already_promoted_knowledge_base(self, managed_registered):
        """Comparing managed against managed is the same call twice at twice the
        price."""
        managed = managed_registered(StubBackend([_chunk("doc-m")]))
        legacy = StubBackend([_chunk("doc-a")])

        await _search(
            legacy, {DUAL_READ_ATTR: True, "retrievalEngine": ENGINE_MANAGED}
        )

        assert managed.calls == []

    def test_start_managed_read_outside_an_event_loop_returns_none(self):
        """Rather than raising. A synchronous caller getting no dual read is a
        missing observation; a synchronous caller getting a RuntimeError is a
        broken retrieval."""
        assert start_managed_read({DUAL_READ_ATTR: True}, ASSISTANT_ID, "q") is None


# ── Legacy is what is served ─────────────────────────────────────────────────
class TestLegacyIsAlwaysServed:
    @pytest.mark.asyncio
    async def test_the_managed_result_never_reaches_the_caller(self, managed_registered):
        """Requirement 18.2. The managed backend returns *different* documents, so
        a facade that served them would be caught by identity rather than by
        count."""
        managed_registered(StubBackend([_chunk("doc-managed-1"), _chunk("doc-managed-2")]))
        legacy = StubBackend([_chunk("doc-legacy-1")])

        results, _, _ = await _search(legacy, {DUAL_READ_ATTR: True})

        assert [r["metadata"]["document_id"] for r in results] == ["doc-legacy-1"]

    @pytest.mark.asyncio
    async def test_an_empty_legacy_result_is_served_as_empty(self, managed_registered):
        """An empty legacy result is a *finding*. Substituting the other engine's
        answer would destroy the measurement and change what users see in the same
        move."""
        managed_registered(StubBackend([_chunk("doc-managed-1")]))
        legacy = StubBackend([])

        results, _, _ = await _search(legacy, {DUAL_READ_ATTR: True})

        assert results == []

    @pytest.mark.asyncio
    async def test_a_managed_failure_does_not_fail_the_turn(self, managed_registered):
        managed_registered(StubBackend(error=RuntimeError("bedrock threw")))
        legacy = StubBackend([_chunk("doc-legacy-1")])

        results, _, emit_count = await _search(legacy, {DUAL_READ_ATTR: True})

        assert len(results) == 1
        emit_count.assert_called()
        assert emit_count.call_args.args[0] == "KbDualReadFailed"

    @pytest.mark.asyncio
    async def test_a_legacy_failure_still_fails_closed(self, managed_registered):
        """And does not leave the managed task orphaned with an unretrieved
        exception.

        ``managed.calls`` is empty because the facade cancels the task it started
        when legacy raised before the comparison was detached. Without that
        cancel the task runs to completion — paying for a Retrieve nobody will
        ever read — and Python reports a task whose exception was never
        retrieved.
        """
        managed = managed_registered(StubBackend([_chunk("doc-managed-1")]))
        legacy = StubBackend(error=RuntimeError("s3 vectors threw"))

        results, _, _ = await _search(legacy, {DUAL_READ_ATTR: True})

        assert results == []
        assert managed.calls == [], (
            "the orphaned managed read was left running after legacy failed"
        )


# ── Latency ──────────────────────────────────────────────────────────────────
class TestThePilotAddsNoLatency:
    @pytest.mark.asyncio
    async def test_the_caller_does_not_wait_for_the_managed_read(self, managed_registered):
        """Requirement 18.5, asserted on the clock.

        The managed backend sleeps for three seconds. If the facade gathers the
        two calls instead of detaching the comparison, this takes three seconds
        and fails; the results themselves would still be correct.
        """
        managed_registered(StubBackend([_chunk("doc-managed-1")], delay=MANAGED_DELAY_SECONDS))
        legacy = StubBackend([_chunk("doc-legacy-1")])

        with patch.dict(
            "os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE_NAME}, clear=False
        ), patch(
            "apis.shared.assistants.rag_service.load_record",
            return_value={DUAL_READ_ATTR: True},
        ), patch(
            "apis.shared.assistants.rag_service.resolve_backend", return_value=legacy
        ), patch(
            "apis.shared.assistants.rag_service.boto3.resource",
            return_value=_all_documents_complete(),
        ), patch(
            "apis.shared.assistants.rag_service.emit_count"
        ), patch(
            "apis.shared.kb_backend.dual_read.emit_value"
        ):
            started = time.perf_counter()
            results = await search_assistant_knowledgebase_with_formatting(
                ASSISTANT_ID, "q", 5, access=ACCESS
            )
            elapsed = time.perf_counter() - started

        assert len(results) == 1
        assert elapsed < MANAGED_DELAY_SECONDS / 3, (
            f"the facade took {elapsed:.2f}s while the managed backend slept "
            f"{MANAGED_DELAY_SECONDS}s; the dual read is being awaited, so every "
            f"piloted turn now pays the slower backend's latency"
        )

    @pytest.mark.asyncio
    async def test_the_managed_read_starts_before_legacy_resolves(self, managed_registered):
        """Concurrency is not just about the total: the two latencies are only
        comparable if both calls were in flight at the same time."""
        order: List[str] = []

        class OrderedLegacy(StubBackend):
            async def search(self, kb_ref, query, top_k=DEFAULT_TOP_K):
                order.append("legacy-start")
                await asyncio.sleep(0.05)
                order.append("legacy-end")
                return [_chunk("doc-legacy-1")]

        class OrderedManaged(StubBackend):
            async def search(self, kb_ref, query, top_k=DEFAULT_TOP_K):
                order.append("managed-start")
                return [_chunk("doc-managed-1")]

        managed_registered(OrderedManaged())
        await _search(OrderedLegacy(), {DUAL_READ_ATTR: True})

        assert "managed-start" in order
        assert order.index("managed-start") < order.index("legacy-end"), (
            f"the managed read started only after legacy finished: {order}"
        )


# ── The comparison itself ────────────────────────────────────────────────────
class TestComparisonMetrics:
    @pytest.mark.asyncio
    async def test_overlap_rank_correlation_and_both_latencies_are_recorded(
        self, managed_registered
    ):
        """Requirement 18.3, all three named measures."""
        shared = [_chunk("doc-a", 0), _chunk("doc-b", 1)]
        managed_registered(StubBackend(shared))
        legacy = StubBackend(shared)

        _, emit_value, _ = await _search(legacy, {DUAL_READ_ATTR: True})

        emitted = [call.args[0] for call in emit_value.call_args_list]
        assert "KbDualReadOverlap" in emitted
        assert "KbDualReadRankCorrelation" in emitted
        assert emitted.count("KbDualReadLatency") == 2

        backends = {
            call.kwargs["dimensions"]["backend"]
            for call in emit_value.call_args_list
            if call.args[0] == "KbDualReadLatency"
        }
        assert backends == {"s3vectors", ENGINE_MANAGED}

    @pytest.mark.asyncio
    async def test_latency_is_published_in_milliseconds(self, managed_registered):
        """A latency published as ``Count`` is not merely mislabelled — CloudWatch
        graphs and alarms on it as a rate."""
        managed_registered(StubBackend([_chunk("doc-a")]))
        _, emit_value, _ = await _search(StubBackend([_chunk("doc-a")]), {DUAL_READ_ATTR: True})

        for call in emit_value.call_args_list:
            if call.args[0] == "KbDualReadLatency":
                assert call.kwargs["unit"] == "Milliseconds"


class TestCompareIsPureArithmetic:
    def test_identical_results_agree_completely(self):
        chunks = [_chunk("doc-a", 0), _chunk("doc-b", 1), _chunk("doc-c", 2)]
        result = compare(chunks, chunks, 100.0, 700.0)

        assert result.overlap_count == 3
        assert result.overlap_ratio == 1.0
        assert result.rank_correlation == pytest.approx(1.0)
        assert result.legacy_ms == 100.0
        assert result.managed_ms == 700.0

    def test_a_reversed_ranking_correlates_negatively(self):
        forward = [_chunk("doc-a", 0), _chunk("doc-b", 1), _chunk("doc-c", 2)]
        result = compare(forward, list(reversed(forward)), 1.0, 1.0)

        assert result.overlap_ratio == 1.0
        assert result.rank_correlation == pytest.approx(-1.0)

    def test_disjoint_results_have_no_overlap_and_no_correlation(self):
        result = compare([_chunk("doc-a")], [_chunk("doc-z")], 1.0, 1.0)

        assert result.overlap_count == 0
        assert result.overlap_ratio == 0.0
        assert result.rank_correlation is None

    def test_overlap_is_symmetric(self):
        """Jaccard, not a ratio against one side's length — which would read as
        agreement when one backend simply returned fewer documents, the case most
        likely to occur while the managed corpus is still catching up."""
        many = [_chunk("doc-a", 0), _chunk("doc-b", 1), _chunk("doc-c", 2)]
        few = [_chunk("doc-a", 0)]

        forward = compare(many, few, 1.0, 1.0)
        backward = compare(few, many, 1.0, 1.0)

        assert forward.overlap_ratio == pytest.approx(1 / 3)
        assert forward.overlap_ratio == backward.overlap_ratio

    def test_one_shared_document_yields_no_correlation(self):
        """A correlation over a single point is undefined, not 1.0 — reporting 1.0
        would make a pilot on small corpora look like perfect agreement."""
        result = compare(
            [_chunk("doc-a", 0), _chunk("doc-b", 1)],
            [_chunk("doc-a", 0), _chunk("doc-z", 1)],
            1.0,
            1.0,
        )

        assert result.overlap_count == 1
        assert result.rank_correlation is None

    def test_a_repeated_document_is_ranked_by_its_best_chunk(self):
        """Not by its last. Both are one-line implementations and both produce a
        number, so the difference is invisible without an input where they
        disagree: here ``doc-a`` appears at ranks 0 and 2 in legacy and at rank 0
        in managed, and the two backends agree perfectly on best-rank while
        appearing to disagree perfectly on last-rank.
        """
        legacy = [_chunk("doc-a", 0), _chunk("doc-b", 1), _chunk("doc-a", 2)]
        managed = [_chunk("doc-a", 0), _chunk("doc-b", 1)]

        result = compare(legacy, managed, 1.0, 1.0)

        assert result.rank_correlation == pytest.approx(1.0), (
            "ranking by the document's last chunk instead of its best inverts "
            "the measured agreement"
        )

    def test_two_empty_results_do_not_divide_by_zero(self):
        result = compare([], [], 1.0, 1.0)

        assert result.overlap_ratio == 0.0
        assert result.rank_correlation is None

    def test_a_document_with_several_chunks_counts_once(self):
        """Otherwise the correlation measures chunking rather than agreement: a
        document contributing four passages would dominate one contributing a
        single passage."""
        repeated = [_chunk("doc-a", 0), _chunk("doc-a", 1), _chunk("doc-a", 2)]
        result = compare(repeated, [_chunk("doc-a", 0)], 1.0, 1.0)

        assert result.overlap_count == 1
        assert result.overlap_ratio == 1.0
        assert result.legacy_count == 3
