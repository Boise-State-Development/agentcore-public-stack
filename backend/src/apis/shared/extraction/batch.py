"""Running one field set over N documents, concurrently and bounded.

``docs/specs/structured-extraction.md`` §17 (Amendment 2026-10-20). The point
of taking N documents in one call is not throughput — it is that the useful
answer is a **judgement across the set**, and a model can only make that if it
sees the whole set in one turn.

WHY A SEMAPHORE AND NOT A BARE ``gather``
-----------------------------------------
An unbounded ``gather`` over fifty documents fires fifty constrained Bedrock
calls at once. This deployment already has a Bedrock throttle alarm, and a
too-eager default would trip it on the first real batch — turning a feature
that works on a demo into one that fails on the day it matters. The limit is
configurable precisely so it can be raised on evidence rather than on hope
(:func:`concurrency_limit`).

WHY THE FIRST DOCUMENT RUNS ALONE
---------------------------------
**Measured, not assumed** (§16.11). Five concurrent calls against a cold schema
each paid grammar compilation in full — 15.3–16.0s where the warm median was
2.95s, so none was served a grammar another had warmed. But because they ran
concurrently, wall clock for all five was 16.05s: **the feared N-times-slower
first run does not happen.** What is wasted is Bedrock's compute, N times over,
not the user's time.

So warming is *not* a latency optimization, and this module does not pretend it
is: it costs about three seconds more on a cold schema and one document's
latency on a warm one. It is adopted for two other reasons:

* **Error economy.** A field set that exceeds one of Bedrock's four
  undocumented capacity limits (§16.9) fails in under a second. Warming first
  turns that into one clear error instead of N identical ones, and stops before
  spending the rest of the batch.
* **No N-fold redundant compilation**, against a deployment that already has a
  throttle alarm.

The warming call is **document one doing real work**, not a throwaway, so
nothing is spent to get it.

PARTIAL FAILURE IS THE NORMAL CASE
----------------------------------
One unreadable document in fourteen must not sink the batch. Every document
comes back as either a result or a reason, and the reason reaches the model —
a set where three documents failed silently is a judgement made on eleven
documents while reading like it was made on fourteen.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional, Sequence, Tuple, TypeVar

from .extractor import ExtractionResult
from .field_sets import FieldSet

logger = logging.getLogger(__name__)

#: Documents one call may carry. Generous because the point of the feature is a
#: judgement across a whole intake, and an office's weekly sweep is tens of
#: solicitations rather than thousands. A request over this is refused with the
#: count, rather than silently truncated to the first N — a set quietly reduced
#: from sixty to fifty produces a confident judgement about the wrong set.
MAX_DOCUMENTS = 50

#: Concurrent extraction calls per batch. **Deliberately conservative.**
#:
#: Each call is a constrained Converse request whose input is a whole document,
#: so four in flight is already a meaningful amount of Bedrock traffic from one
#: turn — and this deployment has a Bedrock throttle alarm that an over-eager
#: default would trip on the first real batch, which is the worst possible time
#: to discover the number. Four also means a batch's first wave finishes in
#: roughly the time one document takes, so the wall-clock win over sequential
#: is already most of what there is to win: fourteen documents at four-wide is
#: four waves, not fourteen.
#:
#: **Raise it on evidence, not on impatience.** The evidence is the
#: `DocumentExtract` EMF records plus the absence of throttling across a few
#: real batches; the lever is `DOCUMENT_EXTRACT_CONCURRENCY`, so no deploy is
#: needed to try a different number in one environment.
DEFAULT_CONCURRENCY = 4

#: Hard bound on the configured value. A deployment that sets 200 has made a
#: typo, not a decision, and the throttle alarm should not be how it finds out.
MAX_CONCURRENCY = 16


def concurrency_limit() -> int:
    """Concurrent extraction calls per batch, from the environment.

    Clamped to ``1..MAX_CONCURRENCY`` and falling back to the default on
    anything unparseable, because a malformed value should narrow the blast
    radius rather than widen it.
    """
    raw = os.environ.get("DOCUMENT_EXTRACT_CONCURRENCY", "").strip()
    if not raw:
        return DEFAULT_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "DOCUMENT_EXTRACT_CONCURRENCY=%r is not an integer; using %d",
            raw,
            DEFAULT_CONCURRENCY,
        )
        return DEFAULT_CONCURRENCY
    if value < 1:
        return 1
    if value > MAX_CONCURRENCY:
        logger.warning(
            "DOCUMENT_EXTRACT_CONCURRENCY=%d exceeds the %d ceiling; clamping",
            value,
            MAX_CONCURRENCY,
        )
        return MAX_CONCURRENCY
    return value


@dataclass(frozen=True)
class DocumentOutcome:
    """One document's result, or the reason there isn't one.

    Both halves travel, because a document that failed has to be visible: a
    judgement across a set is wrong if it was silently made across a subset.
    """

    upload_id: str
    filename: str
    #: ``None`` when the extraction did not produce rows.
    result: Optional[ExtractionResult] = None
    #: Human-readable, model-facing reason. ``None`` on success.
    error: Optional[str] = None
    #: The document's unit texts, kept so provenance is verified against the
    #: whole document even when extraction was narrowed by ``pages``.
    fmt: Optional[str] = None
    texts: Tuple[str, ...] = ()
    #: True when the failure is a property of the **field set**, not of this
    #: document — a capacity limit Bedrock will refuse identically for every
    #: document in the batch. The batch stops rather than producing N copies of
    #: an error about someone else's authoring that the user cannot act on.
    fatal_for_batch: bool = False

    @property
    def ok(self) -> bool:
        return self.result is not None


T = TypeVar("T")


async def run_bounded(
    tasks: Sequence[Callable[[], Awaitable[T]]],
    *,
    limit: Optional[int] = None,
) -> List[T]:
    """Run *tasks* concurrently, at most ``limit`` at a time, in input order.

    A semaphore rather than a chunked loop: chunking makes every wave wait for
    its slowest member, so one 300-page document in a batch of four stalls
    three idle slots. A semaphore starts the next document the moment any slot
    frees.

    Exceptions are returned in place, not raised, so one document cannot sink
    the batch. Callers wrap their own work and decide what a failure means;
    ``asyncio.CancelledError`` is re-raised, because a cancelled turn is not a
    per-document failure.
    """
    if not tasks:
        return []
    semaphore = asyncio.Semaphore(limit or concurrency_limit())

    async def guarded(make: Callable[[], Awaitable[T]]) -> T:
        async with semaphore:
            return await make()

    results = await asyncio.gather(
        *[guarded(make) for make in tasks], return_exceptions=True
    )
    for result in results:
        if isinstance(result, asyncio.CancelledError):
            raise result
    return list(results)  # type: ignore[arg-type]


async def extract_over_documents(
    field_set: FieldSet,
    documents: Sequence[Tuple[str, str]],
    extract_one: Callable[[str, str], Awaitable[DocumentOutcome]],
    *,
    limit: Optional[int] = None,
) -> List[DocumentOutcome]:
    """One field set over N documents: warm on the first, then fan out.

    ``documents`` is ``[(upload_id, label)]`` in the order the caller wants the
    results, and ``extract_one`` does the per-document work — reading it,
    calling the model, verifying. This module owns only the *shape* of the
    concurrency, which is what keeps it testable without a model.

    The first document runs alone so a capacity failure costs one call rather
    than N (§16.9, §16.11). If it fails **because the field set itself cannot
    be compiled**, the batch stops there and that one reason is returned for
    every document: firing the rest would produce N copies of an error about
    the author's field set, none of which the user can act on differently. Any
    other first-document failure is just one bad document, so the rest proceed.
    """
    if not documents:
        return []

    first_id, first_label = documents[0]
    first = await extract_one(first_id, first_label)
    outcomes = [first]

    rest = documents[1:]
    if not rest:
        return outcomes

    if first.error and first.fatal_for_batch:
        logger.warning(
            "document_extract: field set %r cannot be compiled; skipping the "
            "remaining %d document(s)",
            field_set.name,
            len(rest),
        )
        return outcomes + [
            DocumentOutcome(
                upload_id=uid,
                filename=label,
                error=first.error,
                fatal_for_batch=True,
            )
            for uid, label in rest
        ]

    results = await run_bounded(
        [
            (lambda uid=uid, label=label: extract_one(uid, label))
            for uid, label in rest
        ],
        limit=limit,
    )
    for (uid, label), result in zip(rest, results):
        if isinstance(result, BaseException):
            logger.warning(
                "document_extract: %s failed in the batch: %s", uid, result
            )
            outcomes.append(
                DocumentOutcome(
                    upload_id=uid, filename=label, error=f"extraction failed: {result}"
                )
            )
        else:
            outcomes.append(result)
    return outcomes


__all__ = [
    "DEFAULT_CONCURRENCY",
    "RELIABLE_DOCUMENT_COUNT",
    "MAX_CONCURRENCY",
    "MAX_DOCUMENTS",
    "DocumentOutcome",
    "concurrency_limit",
    "extract_over_documents",
    "run_bounded",
]


#: Documents the reduce step handles reliably in one turn. **Measured over ten
#: trials per condition** (§17.8, which supersedes §17.7's three): with a
#: planted cross-document collision the model found it 10/10 at ten documents
#: and 8/10 at both twenty-five and fifty. So this is the threshold above which
#: the number stops being 10/10, not a cliff — the fall-off beyond it is gentle,
#: and §17.7's "roughly a coin flip" was three-trial noise.
#:
#: The binding constraint on a batch is still the model's attention rather than
#: cost: token cost is flat at ~325 per document, which is the opposite of the
#: intuition and the reason this constant exists at all.
#:
#: Not a cap. A user may legitimately want fifty documents extracted as a data
#: pull even where the judgement across them is weaker, and refusing that would
#: trade a useful spreadsheet for a principle. It is the threshold above which
#: the tool tells the model to work the comparison field-by-field — a note that
#: moved collision detection from 5/10 to 8/10 at fifty documents and 8/10 to
#: 9/10 at twenty-five. Consistent direction, largest where the problem is
#: worst, and **not** significant at ten trials: the intervals overlap. It is
#: kept because it is free and aimed at the right failure, not because it is
#: proven.
RELIABLE_DOCUMENT_COUNT = 10
