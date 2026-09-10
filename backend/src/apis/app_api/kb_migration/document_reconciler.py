"""Dead-letter reconciler for stranded managed-KB documents.

Task 16.5 (HANDOFF §5.37). The companion to :mod:`reconciler`, which reconciles
whole knowledge bases against ``ListKnowledgeBases``; this one reconciles
individual ``DOC#`` rows against Bedrock's *document* view.

The gap it closes
-----------------
On the managed path a document's ``DOC#`` status is written by exactly one writer:
the ingestion consumer (``kb_migration/ingestion_consumer.py``). That consumer
polls Bedrock until the document is genuinely retrievable and only then writes
``complete``. It is the right design — but it is the *only* writer, and it runs
inside a Lambda whose asynchronous retry is capped at **2** attempts (a hard
service limit). When an event exhausts those retries and dead-letters, the row is
left in a non-terminal state (``uploading`` / ``chunking`` / ``embedding``) with
**nothing left to revisit it** — even though Bedrock frequently finished indexing
the document seconds after the final attempt was dead-lettered, so the content is
sitting in the knowledge base fully retrievable.

That combination is invisible and permanent, because the retrieval status filter
(``rag_service._filter_vectors_by_document_status``) serves **only** ``complete``
documents. A stranded row's chunks are dropped from every query: the user was told
their upload worked, the content really is in the knowledge base, and the
assistant will never cite it. Two such documents occurred in dev and both needed a
manual DynamoDB edit.

This module is the missing second writer. Once a day it finds ``DOC#`` rows stuck
non-terminal past a grace period, asks Bedrock the ground truth for each, and — the
§5.37 case — drives a stranded-but-retrievable document to ``complete``. It also
handles the two neighbouring outcomes the same probe reveals: a document Bedrock
reports ``FAILED`` is driven to ``failed`` (it was never going to recover), and a
document Bedrock has never heard of (``NOT_FOUND`` — dead-lettered *before* the
ingest was ever accepted) is **re-ingested** from the bytes still in S3. That
re-ingest is the one-click retry of task 14.4 arriving on a schedule instead of a
button.

Ground truth comes from the consumer, not a second copy
-------------------------------------------------------
Every decision here reuses the consumer's own probes — :func:`document_status`
(``GetKnowledgeBaseDocuments``), the ``equals``-on-``document_id`` retrievability
search, its status-set constants, and its terminal-write function. That reuse is
deliberate: those functions carry three hard-won lessons (§5.37 the poll budget,
§5.38 that an unfiltered retrievability search finds the wrong document, §5.39
that the live service returns statuses the SDK enum omits). A reconciler that
re-derived any of them would be a fourth place for the same bug to live. The one
thing this module does *not* reuse is the consumer's *polling* — it takes a single
retrievability reading per document rather than waiting, because it is sweeping a
fleet, not shepherding one upload.

Report-only, and armed separately
----------------------------------
Modelled on :mod:`reconciler`. It ships **disarmed**: it logs exactly what it
would have done and writes nothing, so its judgement can be checked against real
data before it is trusted to correct records. Arming is one flag,
:data:`FLAG_DOC_RECONCILER_ARMED`, and an **empty string reads as off** — an unset
GitHub Actions variable expands to ``""``. The per-run action limit
(:func:`max_actions_per_run`) applies in **both** modes, so a report never claims
more corrections than an armed run would actually make.

The grace gate is a pure function of the row's own ``updatedAt``, never of
discovery time — the same discipline as the KB reconciler's ``createdAt`` gate. A
row whose ``updatedAt`` cannot be read is left alone: without proof that a document
has been stuck *longer than a legitimate in-flight ingestion could take*, touching
it risks racing an upload that is still, correctly, being worked on.

Import boundary
---------------
Module-level imports are stdlib plus the stdlib-only ``ingestion_consumer`` /
``reconciler`` / ``kb_backend.records`` siblings; ``boto3`` and the heavy
``ManagedKbBackend`` are function-local. DynamoDB is reached through the raw table
resource, matching every other module in this package.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional

from apis.app_api.kb_migration import ingestion_consumer as ic
from apis.shared.kb_backend.metrics import emit_count

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Flags ────────────────────────────────────────────────────────────────────
#
# The arming flag. Absent, empty, or anything not in the truthy set means the
# reconciler reports and corrects nothing. Same allow-list as the KB reconciler
# and the dispatcher: the failure being designed around is a value that is present
# but empty (``bool("")`` is off by luck, ``bool("false")`` is not).
FLAG_DOC_RECONCILER_ARMED = "MANAGED_KB_DOC_RECONCILER_ARMED"

_TRUTHY = frozenset({"1", "true", "yes", "on", "enabled"})

# ── Document statuses ─────────────────────────────────────────────────────────
#
# The non-terminal ``DOC#`` states a stranded document can be parked in. Taken to
# match ``apis/app_api/documents/ingestion/status.py``'s ``DocumentStatus`` literal
# minus its terminal members. ``deleting`` is deliberately NOT here: a
# soft-deleted document is being removed on purpose and must never be resurrected
# to ``complete``.
#
# ``provisioning`` (born-managed, MANAGED_KB_NEW_DEFAULT) is here as the long-
# horizon backstop for a first document whose provisioning job was killed between
# building the knowledge base and handing the document over. It is safe to include
# precisely because the sweep already skips any record with no ``awsKbId`` — while
# the knowledge base does not exist there is nothing to probe, so such a document is
# never even considered. Once it does exist, a document still parked here past the
# grace window probes ``NOT_FOUND`` and is re-ingested from S3, which is exactly the
# correction that is owed.
NON_TERMINAL_STATUSES = frozenset({"provisioning", "uploading", "chunking", "embedding"})

# ── Tunables, resolved at call time ──────────────────────────────────────────
#
# Read inside the functions that use them rather than bound as default arguments:
# a default argument is evaluated once at import, so a test overriding it silently
# gets the production value instead. Same reason the KB reconciler does this.

#: A document younger than this is not yet evidence of a dead-letter — it may still
#: be legitimately in flight. The ingestion consumer waits up to
#: ``INDEXED_POLL_TIMEOUT_SECONDS`` (600 s) inside one invocation, and an event
#: gets 1 + 2 deliveries spread over a few minutes before it dead-letters, so a
#: genuinely-working document can still be non-terminal for ~15 minutes. 60 minutes
#: is comfortably past that, which is the correct direction: the cost of waiting one
#: more daily pass is nil, and the cost of racing an in-flight upload is marking it
#: from under the consumer.
STUCK_MIN_AGE_MINUTES = 60.0

#: Bounds the corrective work of a single run — mark-completes, re-ingests and
#: fails together — so a bug in the join, or a sudden flood of stranded rows,
#: costs at most this many actions before someone reads the report. Applied in
#: report-only mode too, so the report is trustworthy.
MAX_ACTIONS_PER_RUN = 25

#: Hard ceiling above which the env override is ignored. A larger sweep should
#: require repeated observed runs, not a variable edit.
MAX_ACTIONS_CEILING = 100

#: Bounds the join itself. A reconciler that walked an unbounded table would time
#: out mid-pass and produce a partial report indistinguishable from a complete one.
MAX_RECORDS_PER_RUN = 5000

# ── Action kinds ──────────────────────────────────────────────────────────────
ACTION_MARK_COMPLETE = "mark_complete"
ACTION_RE_INGEST = "re_ingest"
ACTION_MARK_FAILED = "mark_failed"

# ── Metrics ──────────────────────────────────────────────────────────────────
METRIC_STRANDED_FOUND = "KbStrandedDocumentsFound"
METRIC_MARKED_COMPLETE = "KbStrandedDocumentsCompleted"
METRIC_RE_INGESTED = "KbStrandedDocumentsReingested"
METRIC_MARKED_FAILED = "KbStrandedDocumentsFailed"
METRIC_LIMIT_REACHED = "KbDocumentReconcilerLimitReached"


@dataclass
class PlannedAction:
    """One correction the reconciler intends, and the evidence for it."""

    assistant_id: str
    document_id: str
    kind: str
    current_status: str
    bedrock_status: str
    performed: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "assistantId": self.assistant_id,
            "documentId": self.document_id,
            "kind": self.kind,
            "currentStatus": self.current_status,
            "bedrockStatus": self.bedrock_status,
            "performed": self.performed,
            "error": self.error,
        }


@dataclass
class DocumentReconcileReport:
    """What one run found and what it did (or, disarmed, would have done).

    ``armed`` lives on the report, not only in the logs, so a stored artifact is
    self-describing: an operator reading last night's output should not have to go
    and check what the flag was set to at the time.
    """

    armed: bool = False
    managed_records: int = 0
    documents_scanned: int = 0
    stranded: int = 0
    planned_actions: List[PlannedAction] = field(default_factory=list)
    skipped_too_young: List[str] = field(default_factory=list)
    skipped_in_flight: List[str] = field(default_factory=list)
    skipped_not_retrievable: List[str] = field(default_factory=list)
    limit_reached: bool = False

    @property
    def actions_performed(self) -> int:
        return sum(1 for action in self.planned_actions if action.performed)

    def _count(self, kind: str) -> int:
        return sum(1 for action in self.planned_actions if action.kind == kind)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "armed": self.armed,
            "mode": "armed" if self.armed else "report-only",
            "managedRecords": self.managed_records,
            "documentsScanned": self.documents_scanned,
            "stranded": self.stranded,
            "plannedActions": [action.to_dict() for action in self.planned_actions],
            "plannedByKind": {
                ACTION_MARK_COMPLETE: self._count(ACTION_MARK_COMPLETE),
                ACTION_RE_INGEST: self._count(ACTION_RE_INGEST),
                ACTION_MARK_FAILED: self._count(ACTION_MARK_FAILED),
            },
            "actionsPerformed": self.actions_performed,
            "skippedTooYoung": self.skipped_too_young,
            "skippedInFlight": self.skipped_in_flight,
            "skippedNotRetrievable": self.skipped_not_retrievable,
            "limitReached": self.limit_reached,
        }


# ── Flag and tunable readers ─────────────────────────────────────────────────
def doc_reconciler_armed() -> bool:
    """Whether the reconciler may write. Defaults to **off**.

    An empty string is off: an unset repository or environment variable expands to
    ``""`` in GitHub Actions, and stamping a truthiness test on the raw value is
    the exact bug the KB reconciler was bitten by.
    """
    raw = os.environ.get(FLAG_DOC_RECONCILER_ARMED)
    if not raw:
        return False
    return raw.strip().lower() in _TRUTHY


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"{name}={raw!r} is not a number; falling back to {default}")
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"{name}={raw!r} is not an integer; falling back to {default}")
        return default


def stuck_min_age_minutes() -> float:
    return _env_float("MANAGED_KB_DOC_STUCK_MIN_AGE_MINUTES", STUCK_MIN_AGE_MINUTES)


def max_actions_per_run() -> int:
    """The per-run action bound, clamped so the environment cannot lift it.

    The env var may lower the limit but not raise it past
    :data:`MAX_ACTIONS_CEILING`. A bound any variable can set to a million is not a
    bound; this one caps how much a single bad run can churn before its report is
    read.
    """
    requested = _env_int("MANAGED_KB_DOC_RECONCILER_MAX_ACTIONS", MAX_ACTIONS_PER_RUN)
    if requested > MAX_ACTIONS_CEILING:
        logger.warning(
            f"MANAGED_KB_DOC_RECONCILER_MAX_ACTIONS={requested} exceeds the ceiling "
            f"of {MAX_ACTIONS_CEILING}; clamping. Run the reconciler repeatedly "
            f"rather than raising this."
        )
        return MAX_ACTIONS_CEILING
    return max(requested, 0)


def max_records_per_run() -> int:
    return _env_int("MANAGED_KB_DOC_RECONCILER_MAX_RECORDS", MAX_RECORDS_PER_RUN)


# ── DynamoDB plumbing ────────────────────────────────────────────────────────
def _table():
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["DYNAMODB_ASSISTANTS_TABLE_NAME"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def iter_document_records(assistant_id: str) -> Iterator[Dict[str, Any]]:
    """Every ``DOC#`` row for one assistant, paging the query to exhaustion.

    Paged for the same reason the reconciler pages its scans: a truncated read
    would make a stranded document on a later page look absent, and the run would
    silently skip it.
    """
    from boto3.dynamodb.conditions import Key

    table = _table()
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(f"AST#{assistant_id}")
        & Key("SK").begins_with("DOC#"),
    }
    while True:
        response = table.query(**kwargs)
        for item in response.get("Items") or []:
            yield item
        start = response.get("LastEvaluatedKey")
        if not start:
            return
        kwargs["ExclusiveStartKey"] = start


# ── Age gate (pure function of the row's own updatedAt) ──────────────────────
def document_age_minutes(updated_at: Any, now: Optional[datetime] = None) -> Optional[float]:
    """Minutes since the row's ``updatedAt``, or ``None`` if it cannot be read.

    Reuses the KB reconciler's timestamp parser so the accepted shapes (aware
    datetime, ISO string, epoch number) are identical across the feature.
    """
    from apis.app_api.kb_migration.reconciler import parse_aws_timestamp

    stamped = parse_aws_timestamp(updated_at)
    if stamped is None:
        return None
    return ((now or _now()) - stamped).total_seconds() / 60.0


def document_is_stuck_long_enough(
    updated_at: Any,
    now: Optional[datetime] = None,
    min_age_minutes: Optional[float] = None,
) -> bool:
    """Whether a non-terminal row has been stuck long enough to reconcile.

    A missing or unparseable ``updatedAt`` returns ``False`` — fail-safe. Without
    proof the row has been stuck longer than a legitimate ingestion can take,
    correcting it risks racing the consumer that is still, correctly, working it.
    The input is the row's own ``updatedAt``, never discovery time: the answer must
    not depend on when this process happened to look.
    """
    if min_age_minutes is None:
        min_age_minutes = stuck_min_age_minutes()
    from apis.app_api.kb_migration.reconciler import parse_aws_timestamp

    stamped = parse_aws_timestamp(updated_at)
    if stamped is None:
        return False
    return (now or _now()) - stamped > timedelta(minutes=min_age_minutes)


# ── Retrievability: a single reading, not a poll ─────────────────────────────
def is_retrievable(backend: Any, kb_ref: str, document_id: str) -> bool:
    """Whether a retrieval really returns ``document_id`` right now.

    ONE reading, not the consumer's poll: this sweeps a fleet, so waiting per
    document would blow the run's time budget. The ``equals`` filter on
    ``document_id`` is the load-bearing part and is the whole reason §5.38 exists
    — an *unfiltered* search for a document id returns whatever the reranker
    prefers (a document id is meaningless to an embedding model), so it confirms
    the wrong document as retrievable and gets worse as a knowledge base grows.
    Filtered, a non-empty result *is* proof and an empty one is a true negative.

    A probe that itself errors is treated as "not retrievable" for this pass — the
    reconciler simply leaves the row for the next run rather than acting on a
    failed reading.
    """
    import asyncio

    document_filter = {"equals": {"key": "document_id", "value": document_id}}
    try:
        chunks = asyncio.run(
            backend.search(kb_ref, document_id, 5, retrieval_filter=document_filter)
        )
    except Exception as exc:  # noqa: BLE001 - a probe failure is not a verdict
        logger.warning(f"retrievability probe for {document_id} failed: {exc}")
        return False

    # The filter already restricts the result set to this document; the per-chunk
    # check is a belt-and-braces guard against a filter a future API change ignores.
    for chunk in chunks or []:
        metadata = getattr(chunk, "metadata", None) or {}
        if metadata.get("document_id") == document_id:
            return True
    return False


# ── The run ──────────────────────────────────────────────────────────────────
def reconcile_documents(
    client=None,
    backend_factory: Optional[Callable[[str], Any]] = None,
    armed: Optional[bool] = None,
    now: Optional[datetime] = None,
) -> DocumentReconcileReport:
    """One document-reconciliation pass.

    ``armed`` defaults to :func:`doc_reconciler_armed`, i.e. to the flag, i.e. to
    off. It is an argument only so a test can exercise the armed path without
    mutating process environment — never so a caller can conveniently turn writing
    on.

    ``backend_factory`` builds a :class:`ManagedKbBackend` for an assistant; it is
    injectable so tests can supply a stub that models Bedrock's document view. The
    default constructs a real backend keyed on the assistant id (``App_KB_Id`` ==
    ``assistant_id`` in this phase) with the injected control-plane ``client``.
    """
    from apis.shared.kb_backend.records import ENGINE_MANAGED, resolve_engine

    if armed is None:
        armed = doc_reconciler_armed()
    if now is None:
        now = _now()
    if backend_factory is None:
        backend_factory = _default_backend_factory(client)

    report = DocumentReconcileReport(armed=armed)
    record_limit = max_records_per_run()
    action_limit = max_actions_per_run()
    min_age = stuck_min_age_minutes()

    for record in _iter_managed_records(record_limit, report):
        if resolve_engine(record) != ENGINE_MANAGED:
            continue
        if not record.get("awsKbId"):
            # Managed but not provisioned yet: there is no knowledge base to probe,
            # so a non-terminal document here is waiting on provisioning, not
            # dead-lettered.
            continue

        assistant_id = _assistant_id_of(record)
        if not assistant_id:
            continue
        report.managed_records += 1
        backend = None  # built lazily, only if this assistant has a stranded row

        for document in iter_document_records(assistant_id):
            report.documents_scanned += 1
            status = str(document.get("status") or "")
            if status not in NON_TERMINAL_STATUSES:
                continue

            document_id = str(document.get("documentId") or "")
            if not document_id:
                logger.warning(
                    f"skipping malformed DOC# row {document.get('PK')}/"
                    f"{document.get('SK')}: no documentId"
                )
                continue

            if not document_is_stuck_long_enough(
                document.get("updatedAt"), now=now, min_age_minutes=min_age
            ):
                report.skipped_too_young.append(document_id)
                continue

            report.stranded += 1

            if backend is None:
                backend = backend_factory(assistant_id)

            if report.limit_reached or len(report.planned_actions) >= action_limit:
                report.limit_reached = True
                emit_count(METRIC_LIMIT_REACHED)
                logger.warning(
                    f"per-run action limit of {action_limit} reached; {document_id} "
                    f"and any further stranded documents are left for the next run"
                )
                break

            _reconcile_one(
                assistant_id, document, document_id, status, backend, armed, report
            )

        if report.limit_reached:
            break

    if report.stranded:
        emit_count(METRIC_STRANDED_FOUND, value=report.stranded)

    logger.info(
        f"document reconcile complete: mode={'armed' if armed else 'report-only'} "
        f"managedRecords={report.managed_records} "
        f"documentsScanned={report.documents_scanned} stranded={report.stranded} "
        f"planned={len(report.planned_actions)} performed={report.actions_performed} "
        f"tooYoung={len(report.skipped_too_young)} "
        f"inFlight={len(report.skipped_in_flight)} "
        f"notRetrievable={len(report.skipped_not_retrievable)} "
        f"limitReached={report.limit_reached}"
    )
    return report


def _iter_managed_records(
    record_limit: int, report: DocumentReconcileReport
) -> Iterator[Dict[str, Any]]:
    """KB_Records, bounded, so a huge table cannot make the pass run forever.

    Reuses the KB reconciler's ``iter_kb_records`` (the ``SK begins_with KB#``
    scan) rather than a second implementation, so tombstones and key prefixes are
    handled identically.
    """
    from apis.app_api.kb_migration.reconciler import iter_kb_records

    seen = 0
    for record in iter_kb_records():
        if seen >= record_limit:
            report.limit_reached = True
            logger.warning(
                f"stopping the KB_Record walk at {record_limit}; this run is partial"
            )
            return
        seen += 1
        yield record


def _reconcile_one(
    assistant_id: str,
    document: Dict[str, Any],
    document_id: str,
    status: str,
    backend: Any,
    armed: bool,
    report: DocumentReconcileReport,
) -> None:
    """Classify one stranded document against Bedrock and act (or report).

    The classification mirrors the ingestion consumer's own handling exactly,
    because it uses the consumer's status sets — the point of a reconciler is to
    reach the terminal state the dead-lettered invocation would have reached, not
    to invent a new policy.
    """
    bedrock_status, bedrock_updated_at = ic.document_status(backend, assistant_id, document_id)

    # Still indexing: not stranded, just slow. Leave it — a later pass (or a
    # redelivery that beat the DLQ) will finish it. The grace gate makes this rare.
    if bedrock_status in ic.DOC_STATUSES_IN_FLIGHT:
        report.skipped_in_flight.append(document_id)
        logger.info(
            f"document {document_id} is {bedrock_status} in the knowledge base; "
            f"still indexing, leaving it"
        )
        return

    # Terminal failure on Bedrock's side: retrying cannot help, so drive the row to
    # the terminal state the consumer would have written.
    if bedrock_status in ic.DOC_STATUSES_FAILED:
        _plan(
            report, assistant_id, document_id, ACTION_MARK_FAILED, status, bedrock_status,
            armed,
            lambda: ic.set_document_terminal(
                assistant_id, document_id, ic.STATUS_FAILED,
                error=f"the knowledge base reports this document as {bedrock_status}",
            ),
            METRIC_MARKED_FAILED,
            f"[report-only] WOULD mark {document_id} failed ({bedrock_status})",
        )
        return

    # Indexed (fully or partially): if it is genuinely retrievable, this is the
    # §5.37 case — a stranded-but-servable document — and it is driven to complete.
    if bedrock_status in (ic.DOC_STATUS_INDEXED, *ic.DOC_STATUSES_PARTIAL):
        if not is_retrievable(backend, assistant_id, document_id):
            # Indexed but not yet queryable, or the probe failed. Do NOT claim
            # complete: that is exactly the "upload worked but the assistant cannot
            # see it" report the consumer exists to prevent. Leave it for next run.
            report.skipped_not_retrievable.append(document_id)
            logger.info(
                f"document {document_id} is {bedrock_status} but not retrievable "
                f"this pass; leaving it short of complete"
            )
            return
        indexed_at = bedrock_updated_at or ic._now_iso()
        _plan(
            report, assistant_id, document_id, ACTION_MARK_COMPLETE, status, bedrock_status,
            armed,
            lambda: ic.set_document_terminal(
                assistant_id, document_id, ic.STATUS_COMPLETE,
                indexed_at=indexed_at, retrievable_at=ic._now_iso(),
            ),
            METRIC_MARKED_COMPLETE,
            f"[report-only] WOULD mark {document_id} complete ({bedrock_status}, "
            f"confirmed retrievable)",
        )
        return

    # NOT_FOUND: Bedrock never received this document. The event dead-lettered
    # before the ingest was accepted, so the bytes in S3 were never submitted.
    # Re-ingest them — the scheduled form of task 14.4's one-click retry.
    if bedrock_status == ic.DOC_STATUS_NOT_FOUND:
        source = _document_source(document, document_id)
        if source is None:
            logger.warning(
                f"document {document_id} is NOT_FOUND but its row lacks an s3Key; "
                f"cannot re-ingest, leaving it"
            )
            report.skipped_not_retrievable.append(document_id)
            return
        _plan(
            report, assistant_id, document_id, ACTION_RE_INGEST, status, bedrock_status,
            armed,
            lambda: _reingest(backend, assistant_id, source),
            METRIC_RE_INGESTED,
            f"[report-only] WOULD re-ingest {document_id} (NOT_FOUND in the "
            f"knowledge base; bytes still in S3)",
        )
        return

    # Any other value: the live service returns statuses the SDK enum omits
    # (§5.39). Treat unknown as "still working" and leave it, logging the value so
    # it can be classified rather than silently mishandled.
    report.skipped_in_flight.append(document_id)
    logger.warning(
        f"document {document_id} reported unrecognised Bedrock status "
        f"{bedrock_status!r}; treating it as in-flight and leaving it"
    )


def _plan(
    report: DocumentReconcileReport,
    assistant_id: str,
    document_id: str,
    kind: str,
    current_status: str,
    bedrock_status: str,
    armed: bool,
    perform: Callable[[], None],
    metric: str,
    report_only_message: str,
) -> None:
    """Record a planned action and, if armed, perform it.

    The action is appended to the report whether or not it runs, so the report-only
    artifact describes exactly what an armed run would do. When armed, a failure is
    captured on the action rather than raised — one bad document must not end the
    sweep — matching the KB reconciler's per-orphan error handling.
    """
    action = PlannedAction(
        assistant_id=assistant_id,
        document_id=document_id,
        kind=kind,
        current_status=current_status,
        bedrock_status=bedrock_status,
    )
    report.planned_actions.append(action)

    if not armed:
        logger.warning(f"{report_only_message}. Set {FLAG_DOC_RECONCILER_ARMED} to arm.")
        return

    try:
        perform()
        action.performed = True
        emit_count(metric)
        logger.info(f"{kind} performed for document {document_id}")
    except Exception as exc:  # noqa: BLE001 - one bad document must not end the run
        action.error = str(exc)
        logger.error(f"{kind} failed for document {document_id}: {exc}", exc_info=True)


def _reingest(backend: Any, assistant_id: str, source: Any) -> None:
    import asyncio

    asyncio.run(backend.ingest(assistant_id, source))


def _document_source(document: Dict[str, Any], document_id: str) -> Optional[Any]:
    """Reconstruct a ``DocumentSource`` for re-ingest from the ``DOC#`` row.

    The row carries ``s3Key`` and ``filename`` (from the ``Document`` model's
    aliases), which is everything the managed backend needs to re-submit bytes
    already in S3. Returns ``None`` when ``s3Key`` is absent — an old row that
    predates the attribute cannot be re-ingested from here and is left for a
    re-upload.
    """
    from apis.shared.kb_backend.protocol import DocumentSource

    s3_key = document.get("s3Key")
    if not s3_key:
        return None
    filename = document.get("filename") or ""
    return DocumentSource(document_id=document_id, filename=filename, s3_key=str(s3_key))


def _default_backend_factory(client) -> Callable[[str], Any]:
    """Build a real :class:`ManagedKbBackend` per assistant, sharing one client.

    Function-local import: ``ManagedKbBackend`` pulls in the retrieval stack and
    must not be a module-level import in this size-constrained Lambda.
    """
    def factory(_assistant_id: str) -> Any:
        from apis.shared.kb_backend.managed_backend import ManagedKbBackend

        return ManagedKbBackend(agent_client=client)

    return factory


def _assistant_id_of(record: Dict[str, Any]) -> str:
    pk = str(record.get("PK") or "")
    return pk[len("AST#") :] if pk.startswith("AST#") else ""


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Scheduled entry point. Returns the report so it lands in the invocation log.

    The invocation event is deliberately **not** consulted for arming, for the same
    reason as the KB reconciler: an event payload is the one input an operator does
    not review, so honouring an ``armed`` field in it would let any principal
    holding ``lambda:InvokeFunction`` correct records while every reviewable setting
    still said report-only. If the event disagrees with the flag, the flag wins and
    the disagreement is logged.
    """
    requested = (event or {}).get("armed")
    if requested is not None:
        logger.warning(
            f"ignoring armed={requested!r} from the invocation event: arming is "
            f"controlled only by {FLAG_DOC_RECONCILER_ARMED}"
        )
    report = reconcile_documents()
    return {"statusCode": 200, "report": report.to_dict()}


__all__ = [
    "ACTION_MARK_COMPLETE",
    "ACTION_MARK_FAILED",
    "ACTION_RE_INGEST",
    "FLAG_DOC_RECONCILER_ARMED",
    "MAX_ACTIONS_CEILING",
    "MAX_ACTIONS_PER_RUN",
    "MAX_RECORDS_PER_RUN",
    "METRIC_LIMIT_REACHED",
    "METRIC_MARKED_COMPLETE",
    "METRIC_MARKED_FAILED",
    "METRIC_RE_INGESTED",
    "METRIC_STRANDED_FOUND",
    "NON_TERMINAL_STATUSES",
    "STUCK_MIN_AGE_MINUTES",
    "DocumentReconcileReport",
    "PlannedAction",
    "doc_reconciler_armed",
    "document_age_minutes",
    "document_is_stuck_long_enough",
    "is_retrievable",
    "iter_document_records",
    "lambda_handler",
    "max_actions_per_run",
    "max_records_per_run",
    "reconcile_documents",
    "stuck_min_age_minutes",
]
