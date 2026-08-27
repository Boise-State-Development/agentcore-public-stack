"""The dual-read pilot: measure the managed backend against real traffic.

Requirement 18. The rollout should rest on evidence from *our* corpus and *our*
users, not solely on a three-document benchmark. So an opted-in knowledge base
can have both backends answer the same query, with **legacy always served** and
the managed result kept purely as an observation.

Three rules make this safe to leave switched on, and each is a property of the
code rather than an intention:

* **Legacy is what is served.** The managed result never reaches the caller. It
  is not blended, not preferred when it looks better, not used as a fallback when
  legacy is empty — an empty legacy result is a *finding*, and substituting the
  other engine's answer would destroy the measurement and change what users see
  in the same move.
* **The managed call cannot fail the turn.** It runs as a detached task whose
  exceptions are logged and dropped. :func:`observe` has no failure mode that
  propagates.
* **It cannot add user-visible latency (18.5).** Both searches start together and
  the caller is handed the legacy result the moment it resolves; the managed call
  keeps running afterwards on the event loop. This matters concretely: managed
  ``Retrieve`` measured a 662–695 ms p50 against legacy's 257 ms, so anything that
  awaited both would nearly triple the retrieval leg of every piloted turn.

Why the task needs a strong reference
-------------------------------------
``asyncio.create_task`` returns the only strong reference to the task. Drop it and
the task becomes eligible for garbage collection mid-flight, which surfaces as
comparisons that silently stop being recorded under load — the failure mode that
looks like "the pilot found nothing interesting". Hence :data:`_IN_FLIGHT` and the
done-callback that discards from it, which is the documented CPython pattern.

Why the comparison is a pure function
-------------------------------------
:func:`compare` takes two chunk lists and two durations and returns a value. It
touches no clock, no client and no environment, so the ranking mathematics can be
tested without any of the machinery around it — and the machinery can be tested
without asserting on arithmetic.

Feature: managed-kb-migration
Requirements: 18.1, 18.2, 18.3, 18.4, 18.5
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

from apis.shared.kb_backend.metrics import (
    METRIC_DUAL_READ_FAILED,
    METRIC_DUAL_READ_LATENCY,
    METRIC_DUAL_READ_OVERLAP,
    METRIC_DUAL_READ_RANK_CORRELATION,
    emit_count,
    emit_value,
)
from apis.shared.kb_backend.protocol import DEFAULT_TOP_K, Chunk
from apis.shared.kb_backend.records import ENGINE_MANAGED

logger = logging.getLogger(__name__)

#: KB_Record attribute that opts one knowledge base into the pilot. Absence means
#: off (Requirement 18.4), the same convention ``retrievalEngine`` uses: the
#: default costs nothing to express and nothing to revert.
DUAL_READ_ATTR = "dualReadPilot"

#: Strong references to detached comparison tasks. See the module docstring.
_IN_FLIGHT: Set["asyncio.Task[None]"] = set()


def is_pilot_enabled(record: Optional[Mapping[str, Any]]) -> bool:
    """Whether this knowledge base is opted into the pilot.

    Strictly ``is True``: a truthy string left behind by a hand-edited record must
    not enrol a knowledge base into paying for a second retrieval on every turn.
    The same reasoning armed the reconciler's flag, where a permissive read of an
    event field turned a report-only job into a deleting one.
    """
    if not record:
        return False
    return record.get(DUAL_READ_ATTR) is True


@dataclass(frozen=True)
class Comparison:
    """One dual read's observation. Serves nothing; describes everything."""

    legacy_count: int
    managed_count: int
    overlap_count: int
    overlap_ratio: float
    rank_correlation: Optional[float]
    legacy_ms: float
    managed_ms: float

    def as_log_fields(self) -> Dict[str, Any]:
        return {
            "legacyCount": self.legacy_count,
            "managedCount": self.managed_count,
            "overlapCount": self.overlap_count,
            "overlapRatio": round(self.overlap_ratio, 4),
            "rankCorrelation": (
                None if self.rank_correlation is None else round(self.rank_correlation, 4)
            ),
            "legacyMs": round(self.legacy_ms, 1),
            "managedMs": round(self.managed_ms, 1),
        }


def _first_positions(chunks: Sequence[Chunk]) -> Dict[str, int]:
    """Each ``document_id``'s best rank in a result list, 0-based.

    A document can contribute several chunks, so "the document's rank" is the rank
    of its best chunk. Using every chunk instead would let a document with four
    passages dominate a correlation over one with a single passage, which measures
    chunking rather than agreement.
    """
    positions: Dict[str, int] = {}
    for index, chunk in enumerate(chunks):
        document_id = (chunk.metadata or {}).get("document_id") or chunk.document_id
        if document_id and document_id not in positions:
            positions[document_id] = index
    return positions


def _spearman(left: Sequence[float], right: Sequence[float]) -> Optional[float]:
    """Pearson correlation of two rank vectors — Spearman, computed by hand.

    Written out rather than pulled from scipy: this package is bundled into
    size-constrained Lambda images, and a numerical stack is a large dependency to
    add for one dot product.

    ``None`` when fewer than two documents are shared (a correlation over one point
    is undefined, not 1.0) or when either vector has zero variance, which is what
    happens when both backends return the same single document.
    """
    n = len(left)
    if n < 2 or n != len(right):
        return None

    mean_left = sum(left) / n
    mean_right = sum(right) / n
    d_left = [value - mean_left for value in left]
    d_right = [value - mean_right for value in right]

    covariance = sum(a * b for a, b in zip(d_left, d_right))
    variance_left = sum(a * a for a in d_left)
    variance_right = sum(b * b for b in d_right)

    if variance_left == 0 or variance_right == 0:
        return None

    return covariance / ((variance_left**0.5) * (variance_right**0.5))


def compare(
    legacy: Sequence[Chunk],
    managed: Sequence[Chunk],
    legacy_ms: float,
    managed_ms: float,
) -> Comparison:
    """The observation for one dual read (Requirement 18.3). Pure.

    ``overlap_ratio`` is Jaccard — shared documents over the union — chosen because
    it is symmetric. A ratio against one side's length would read as agreement when
    one backend simply returned fewer documents, which is the case most likely to
    occur while the managed corpus is still catching up.
    """
    legacy_positions = _first_positions(legacy)
    managed_positions = _first_positions(managed)

    legacy_ids = set(legacy_positions)
    managed_ids = set(managed_positions)
    shared = legacy_ids & managed_ids
    union = legacy_ids | managed_ids

    ordered = sorted(shared, key=lambda doc_id: legacy_positions[doc_id])
    correlation = _spearman(
        [float(legacy_positions[doc_id]) for doc_id in ordered],
        [float(managed_positions[doc_id]) for doc_id in ordered],
    )

    return Comparison(
        legacy_count=len(legacy),
        managed_count=len(managed),
        overlap_count=len(shared),
        overlap_ratio=(len(shared) / len(union)) if union else 0.0,
        rank_correlation=correlation,
        legacy_ms=legacy_ms,
        managed_ms=managed_ms,
    )


async def _publish(comparison: Comparison) -> None:
    """Record the observation. Metrics go to a thread; they are boto3 calls.

    Off the critical path already, but the event loop is shared with every other
    in-flight turn, so four synchronous HTTP calls would still be four pauses
    everybody pays for.
    """
    logger.info(f"dual read comparison: {comparison.as_log_fields()}")

    def _emit() -> None:
        emit_value(METRIC_DUAL_READ_OVERLAP, comparison.overlap_ratio, unit="Percent")
        if comparison.rank_correlation is not None:
            emit_value(METRIC_DUAL_READ_RANK_CORRELATION, comparison.rank_correlation)
        emit_value(
            METRIC_DUAL_READ_LATENCY,
            comparison.legacy_ms,
            unit="Milliseconds",
            dimensions={"backend": "s3vectors"},
        )
        emit_value(
            METRIC_DUAL_READ_LATENCY,
            comparison.managed_ms,
            unit="Milliseconds",
            dimensions={"backend": ENGINE_MANAGED},
        )

    await asyncio.to_thread(_emit)


async def observe(
    assistant_id: str,
    query: str,
    top_k: int,
    legacy_chunks: List[Chunk],
    legacy_ms: float,
    managed_task: "Optional[asyncio.Task[List[Chunk]]]",
) -> None:
    """Await the already-running managed search and record the comparison.

    Never raises, and never returns anything a caller could serve. ``managed_task``
    is awaited here rather than started here, so that by the time this runs the
    managed call has been in flight for as long as the legacy one took — which is
    what makes the two latencies comparable and the pilot non-additive.
    """
    if managed_task is None:
        return

    started = time.perf_counter()
    try:
        managed_chunks = await managed_task
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # A managed-side failure is a finding, not an incident: the turn was
        # served from legacy before this coroutine ran.
        logger.warning(
            f"dual read: the managed backend failed for assistant {assistant_id}; "
            f"the turn was already served from legacy: {exc}"
        )
        await asyncio.to_thread(emit_count, METRIC_DUAL_READ_FAILED)
        return

    managed_ms = legacy_ms + (time.perf_counter() - started) * 1000.0

    try:
        await _publish(compare(legacy_chunks, managed_chunks, legacy_ms, managed_ms))
    except Exception as exc:  # noqa: BLE001 - observation must not escape
        logger.warning(f"dual read: could not record the comparison: {exc}")


def start_managed_read(
    record: Optional[Mapping[str, Any]],
    assistant_id: str,
    query: str,
    top_k: int = DEFAULT_TOP_K,
) -> "Optional[asyncio.Task[List[Chunk]]]":
    """Launch the observational managed search, or return ``None``.

    ``None`` — meaning "no dual read this turn" — for every one of: the knowledge
    base is not opted in, this build has no managed backend registered, the record
    already names managed as its engine (there would be nothing to compare
    against), or the task could not be created. Each is an ordinary state, so none
    of them logs at error level or raises.

    Called *before* the legacy search is awaited, which is the whole basis of
    Requirement 18.5.
    """
    if not is_pilot_enabled(record):
        return None

    from apis.shared.kb_backend.records import resolve_engine
    from apis.shared.kb_backend.resolver import backend_for_engine

    if resolve_engine(record) == ENGINE_MANAGED:
        # Already promoted: the managed backend is the one being served, so a
        # "comparison" would be the same call twice at twice the price.
        return None

    managed = backend_for_engine(ENGINE_MANAGED)
    if managed is None:
        return None

    try:
        task = asyncio.create_task(managed.search(assistant_id, query, top_k))
    except RuntimeError as exc:
        logger.warning(f"dual read: could not start the managed search: {exc}")
        return None

    _IN_FLIGHT.add(task)
    task.add_done_callback(_IN_FLIGHT.discard)
    return task


def schedule_observation(
    assistant_id: str,
    query: str,
    top_k: int,
    legacy_chunks: List[Chunk],
    legacy_ms: float,
    managed_task: "Optional[asyncio.Task[List[Chunk]]]",
) -> None:
    """Detach :func:`observe` so the caller can return immediately.

    The point of Requirement 18.5 in one function: nothing after this line is
    awaited before the user gets their answer.
    """
    if managed_task is None:
        return

    try:
        observer = asyncio.create_task(
            observe(assistant_id, query, top_k, legacy_chunks, legacy_ms, managed_task)
        )
    except RuntimeError as exc:
        logger.warning(f"dual read: could not schedule the comparison: {exc}")
        managed_task.cancel()
        return

    _IN_FLIGHT.add(observer)
    observer.add_done_callback(_IN_FLIGHT.discard)


__all__ = [
    "DUAL_READ_ATTR",
    "Comparison",
    "compare",
    "is_pilot_enabled",
    "observe",
    "schedule_observation",
    "start_managed_read",
]
