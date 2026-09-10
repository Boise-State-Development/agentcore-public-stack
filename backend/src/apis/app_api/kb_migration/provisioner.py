"""Born-managed provisioning job: build the knowledge base, then own the handoff.

The worker-side half of ``MANAGED_KB_NEW_DEFAULT``. The API-side half
(:mod:`apis.app_api.kb_upgrade.born_managed`) does three conditional writes on the
first document upload — create the record, declare the engine managed, queue this
job — and returns in milliseconds. Everything slow happens here.

Why the ingestion trigger lives in this job and not in the S3 event
-------------------------------------------------------------------
The S3 ``ObjectCreated`` event for the first document fires within seconds of the
client's upload. ``CreateKnowledgeBase`` takes 47–124 s to ``ACTIVE`` and the whole
provision takes minutes. Lambda's asynchronous retry is capped at **2** attempts —
a hard service limit, not a setting — so the event is exhausted and dead-lettered
long before there is a knowledge base to ingest into. That is precisely the §5.37
dead-letter failure that leaves a document permanently invisible, and designing a
new feature that walks into it on its very first document would be a choice.

So the managed ingestion consumer **defers** while a record is born-managed and
unprovisioned (a benign no-op, no dead-letter), and this job ingests the pending
documents itself once the knowledge base exists. Correctness then rests on the
dispatcher's work-key queue and the worker lease — retried until terminal, leased
so two workers cannot both provision — rather than on a two-try event window.

Failure means legacy, never limbo
---------------------------------
Managed intent with no knowledge base is a dead end in both directions: the legacy
pipeline skips the agent's uploads because the record says managed, and the managed
one cannot serve because there is nothing to serve from. So a provisioning failure
does not merely stop — it **removes** ``retrievalEngine``, returning the agent to
legacy-by-absence, and fails the waiting documents with a message that says to try
again. A re-upload then takes the legacy path and works. See :func:`_fall_back_to_legacy`.

Import boundary
---------------
Module-level imports are stdlib only, like every other module in this package: this
code ships in the size-constrained migration Lambda image. Everything heavy —
boto3, the provisioner, the ingestion consumer — is imported inside the functions
that need it.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

METRIC_BORN_MANAGED_PROVISIONED = "KbBornManagedProvisioned"
METRIC_BORN_MANAGED_FAILED = "KbBornManagedFailed"
METRIC_BORN_MANAGED_DOCUMENTS = "KbBornManagedDocuments"

#: Documents ingested per invocation. **One**, deliberately.
#:
#: The worker's Lambda timeout is 15 minutes and one document's ingestion budget is
#: already 10.5 (``INDEXED_POLL_TIMEOUT_SECONDS`` + the retrievable poll), because
#: image-heavy PDFs run the vision model per page. Two documents in one invocation
#: could not both finish, and being killed mid-wait is the one outcome worth
#: engineering away: it costs a whole dispatcher interval and teaches nothing.
#:
#: More than one pending document only happens when the author uploaded again
#: during the provisioning window, so the common case is exactly one. Anything left
#: over re-arms the work key and is picked up on the next tick.
MAX_DOCUMENTS_PER_INVOCATION = 1

#: The user-facing copy for a document orphaned by a failed provision. Written for
#: the person looking at the upload, not for an operator reading a log.
PROVISIONING_FAILED_MESSAGE = (
    "We could not prepare this assistant's knowledge base, so this document was "
    "not added. Please upload it again."
)


def _now_iso() -> str:
    from apis.shared.timestamps import utc_now_iso

    return utc_now_iso()


def _documents_bucket() -> str:
    bucket = os.environ.get("S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME")
    if not bucket:
        raise RuntimeError("S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME is not set")
    return bucket


def pending_documents(assistant_id: str) -> List[Dict[str, Any]]:
    """``DOC#`` rows still waiting on provisioning, oldest first.

    Reads the raw table rather than ``list_assistant_documents`` for the import
    reason in the module docstring, and because that function auto-fails stale rows
    as a side effect of reading them — a write triggered by a query is not something
    this job wants happening underneath it.
    """
    import boto3
    from boto3.dynamodb.conditions import Key

    from apis.app_api.kb_migration.ingestion_consumer import STATUS_PROVISIONING

    table = boto3.resource("dynamodb").Table(os.environ["DYNAMODB_ASSISTANTS_TABLE_NAME"])
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(f"AST#{assistant_id}")
        & Key("SK").begins_with("DOC#"),
    }
    while True:
        response = table.query(**kwargs)
        items.extend(response.get("Items") or [])
        last = response.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last

    waiting = [
        item for item in items if str(item.get("status") or "") == STATUS_PROVISIONING
    ]
    waiting.sort(key=lambda item: str(item.get("createdAt") or ""))
    return waiting


async def run_born_managed(
    assistant_id: str,
    app_kb_id: str,
    record: Dict[str, Any],
):
    """Provision the knowledge base and ingest the documents waiting on it.

    Returns the worker's :class:`~apis.app_api.kb_migration.worker.StepResult`.
    Called under the worker's lease, from :func:`worker.run_step`, for a record in
    ``migrationState = born_managed``.

    Idempotent at every step, because the dispatcher will bring this record back
    until it is terminal and a 15-minute Lambda can be killed at any point:

    * ``provision_managed_kb`` resumes from the record's persisted ``clientToken``,
      so a retry adopts the half-created knowledge base instead of making a second.
    * ``handle_object`` probes Bedrock before ingesting, so a re-run of a document
      already submitted waits on it rather than re-submitting it.
    * The terminal transition is the last write, so anything killed before it is
      simply re-run.
    """
    from apis.shared.kb_backend import records as r
    from apis.shared.kb_backend.metrics import emit_count
    from apis.app_api.kb_migration.worker import StepResult

    generation = int(record.get("migrationGeneration") or 0)

    if r.resolve_engine(record) != r.ENGINE_MANAGED:
        # A previous attempt already fell back to legacy but was killed before it
        # could clear the work keys. Finish that, rather than provisioning a
        # knowledge base for a record that has stopped pointing at one.
        logger.info(
            f"kb {app_kb_id}: born-managed record is back on legacy; closing out "
            f"the abandoned provisioning job"
        )
        await _finish(assistant_id, app_kb_id, generation, r.MIGRATION_FAILED,
                      reason="provisioning already rolled back to legacy")
        return StepResult(
            assistant_id, app_kb_id, r.BORN_MANAGED, r.MIGRATION_FAILED,
            detail="already rolled back to legacy",
        )

    provisioned = await _provision(assistant_id, app_kb_id, record)
    if provisioned is None:
        # Another worker holds the provisioning. Ours re-arms and steps aside
        # rather than creating a second knowledge base.
        await _rearm(assistant_id, app_kb_id, generation)
        return StepResult(
            assistant_id, app_kb_id, r.BORN_MANAGED, r.BORN_MANAGED,
            detail="provisioning already in progress elsewhere; re-queued",
        )
    if provisioned is False:
        emit_count(METRIC_BORN_MANAGED_FAILED)
        return StepResult(
            assistant_id, app_kb_id, r.BORN_MANAGED, r.MIGRATION_FAILED,
            detail="provisioning failed; the agent is back on legacy",
        )

    emit_count(METRIC_BORN_MANAGED_PROVISIONED)

    try:
        return await _hand_off(assistant_id, app_kb_id, generation)
    except Exception as exc:  # noqa: BLE001
        # The knowledge base exists but the handoff broke in a way this job did not
        # anticipate. Falling back is still the right move and this clause is what
        # guarantees it: letting the exception reach `worker.run_step` would send the
        # record to `failed` with `retrievalEngine` still set — managed intent, no
        # usable pipeline, and no work keys left to fix it. That is the one outcome
        # this design exists to make impossible.
        logger.error(
            f"kb {app_kb_id}: born-managed handoff failed after provisioning: {exc}",
            exc_info=True,
        )
        await _fall_back_to_legacy(assistant_id, app_kb_id, record, reason=str(exc))
        emit_count(METRIC_BORN_MANAGED_FAILED)
        return StepResult(
            assistant_id, app_kb_id, r.BORN_MANAGED, r.MIGRATION_FAILED,
            detail="handoff failed after provisioning; the agent is back on legacy",
        )


async def _hand_off(assistant_id: str, app_kb_id: str, generation: int):
    """Ingest the documents that were deferred while the knowledge base was built."""
    from apis.shared.kb_backend import records as r
    from apis.shared.kb_backend.metrics import emit_count
    from apis.app_api.kb_migration.worker import StepResult

    waiting = await _to_thread(pending_documents, assistant_id)
    if not waiting:
        # Nothing is waiting: the knowledge base is built and the ordinary managed
        # path (S3 event → ingestion consumer) owns every upload from here.
        await _finish(assistant_id, app_kb_id, generation, r.RETAIN)
        return StepResult(
            assistant_id, app_kb_id, r.BORN_MANAGED, r.RETAIN,
            converged=True, detail="knowledge base ready; no documents waiting",
        )

    ingested = await _ingest_waiting(assistant_id, waiting[:MAX_DOCUMENTS_PER_INVOCATION])
    remaining = len(waiting) - MAX_DOCUMENTS_PER_INVOCATION

    if remaining > 0:
        await _rearm(assistant_id, app_kb_id, generation)
        return StepResult(
            assistant_id, app_kb_id, r.BORN_MANAGED, r.BORN_MANAGED,
            documents_migrated=ingested,
            detail=f"{remaining} document(s) still waiting; re-queued",
        )

    await _finish(assistant_id, app_kb_id, generation, r.RETAIN)
    if ingested:
        emit_count(METRIC_BORN_MANAGED_DOCUMENTS, ingested)
    return StepResult(
        assistant_id, app_kb_id, r.BORN_MANAGED, r.RETAIN,
        documents_migrated=ingested, converged=True,
        detail="knowledge base ready and the first document ingested",
    )


async def _provision(assistant_id: str, app_kb_id: str, record: Dict[str, Any]):
    """Build the knowledge base. ``True`` built, ``None`` someone else's, ``False`` failed.

    A three-valued answer rather than an exception pair because the three outcomes
    need three different things from the caller: carry on, step aside, or fall back
    to legacy. Collapsing "someone else is provisioning" into a failure would roll
    an agent back to legacy while a perfectly good knowledge base was being built
    for it.
    """
    from apis.shared.kb_backend.provisioning import (
        ProvisioningInProgress,
        provision_managed_kb,
    )

    try:
        await provision_managed_kb(
            assistant_id,
            app_kb_id,
            owner_user_id=str(record.get("ownerUserId") or ""),
        )
        return True
    except ProvisioningInProgress as exc:
        logger.info(f"kb {app_kb_id}: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 — every other failure means legacy
        logger.error(
            f"kb {app_kb_id}: born-managed provisioning failed: {exc}", exc_info=True
        )
        await _fall_back_to_legacy(assistant_id, app_kb_id, record, reason=str(exc))
        return False


async def _ingest_waiting(assistant_id: str, waiting: List[Dict[str, Any]]) -> int:
    """Ingest documents whose S3 event was deferred. Returns how many finished.

    Reuses the managed ingestion consumer's ``handle_object`` outright rather than
    reimplementing ingest → wait-indexed → wait-retrievable → terminal. That
    function is where §5.37, §5.38 and §5.39 are encoded — Bedrock reports
    ``INDEXED`` up to a second before a document is retrievable, ``TEXT_INDEXED``
    is not in the SDK's enum, a document already in flight must not be
    re-submitted — plus the authoritative S3-HEAD byte-cap reconcile from
    Requirement 12.3. A second implementation of that would be a second place for
    those lessons to be forgotten.

    The row is moved ``provisioning → uploading`` first, so the author watching the
    upload sees it leave "Provisioning knowledge base…" the moment the wait becomes
    an ordinary indexing wait — and so the stale-document sweep's clock starts from
    the point the document really entered the normal pipeline.
    """
    from apis.app_api.kb_migration import ingestion_consumer as ic

    bucket = _documents_bucket()
    done = 0

    for document in waiting:
        document_id = str(document.get("documentId") or "")
        s3_key = str(document.get("s3Key") or "")
        if not document_id or not s3_key:
            logger.warning(
                f"skipping malformed waiting DOC# row {document.get('SK')!r}: "
                f"documentId or s3Key is missing"
            )
            continue

        await _to_thread(
            ic.set_document_terminal, assistant_id, document_id, "uploading"
        )
        try:
            await _to_thread(ic.handle_object, bucket, s3_key)
            done += 1
        except Exception as exc:  # noqa: BLE001
            # handle_object has already marked the document failed for anything
            # genuinely terminal; what reaches here is its "leave it for
            # redelivery" signal, and there is no redelivery for a deferred event.
            # Leaving the row at `uploading` is correct: this job re-arms, and the
            # document reconciler (16.5) is the longer-horizon backstop.
            logger.warning(
                f"document {document_id} did not finish indexing in this "
                f"invocation; it will be retried: {exc}"
            )

    return done


async def _fall_back_to_legacy(
    assistant_id: str,
    app_kb_id: str,
    record: Dict[str, Any],
    *,
    reason: str,
) -> None:
    """Undo the managed intent so the agent works on legacy again.

    Order matters and is the opposite of intuition — documents first, engine
    second, terminal state last:

    1. **Fail the waiting documents** (and return their byte reservations). Done
       while the record still says managed, because that is the state in which
       these rows are unambiguously this job's to resolve.
    2. **Remove ``retrievalEngine``** (``rollback_engine``). The agent is legacy by
       absence again, byte-identical to one that never tried, so the next upload
       takes the legacy pipeline and simply works.
    3. **Go terminal**, which removes the work keys.

    A crash between 1 and 2, or 2 and 3, leaves the work keys in place, so the
    dispatcher brings the record back and :func:`run_born_managed` re-runs from
    whichever point it reached — its engine check handles the "already rolled back"
    case. A crash the other way round, terminal-state-first, is the one shape that
    would strand the agent: managed intent, no knowledge base, and nothing left in
    the queue to fix it.
    """
    from apis.shared.kb_backend import records as r

    generation = int(record.get("migrationGeneration") or 0)

    try:
        await _fail_waiting_documents(assistant_id)
    except Exception as exc:  # noqa: BLE001 — the rollback matters more
        logger.error(
            f"kb {app_kb_id}: could not fail the documents waiting on provisioning; "
            f"rolling back to legacy anyway: {exc}"
        )

    try:
        await _to_thread(r.rollback_engine, assistant_id, app_kb_id, _now_iso())
        logger.info(f"kb {app_kb_id}: rolled back to legacy after a failed provision")
    except r.TransitionLost:
        # Already not managed. Nothing to undo.
        logger.info(f"kb {app_kb_id}: engine was already legacy at rollback")

    await _finish(assistant_id, app_kb_id, generation, r.MIGRATION_FAILED, reason=reason)


async def _fail_waiting_documents(assistant_id: str) -> None:
    """Mark every document waiting on provisioning failed, and return its bytes."""
    from apis.app_api.kb_migration import ingestion_consumer as ic

    for document in await _to_thread(pending_documents, assistant_id):
        document_id = str(document.get("documentId") or "")
        if not document_id:
            continue
        await _to_thread(
            ic.set_document_terminal,
            assistant_id,
            document_id,
            ic.STATUS_FAILED,
            None,
            None,
            PROVISIONING_FAILED_MESSAGE,
        )
        # The request-time reservation would otherwise leak: nothing else will ever
        # settle a document whose ingestion never happened. settle_once keeps this
        # exactly-once against the stale sweep reaching the same row.
        await _to_thread(
            ic._release_reservation,
            assistant_id,
            document_id,
            ic._declared_bytes(document),
        )


async def _rearm(assistant_id: str, app_kb_id: str, generation: int) -> None:
    """Keep the job queued, due now, without changing state."""
    from apis.shared.kb_backend import records as r

    try:
        await _to_thread(
            r.set_migration_state,
            assistant_id,
            app_kb_id,
            r.BORN_MANAGED,
            generation,
            _now_iso(),
        )
    except r.TransitionLost:
        logger.info(f"kb {app_kb_id}: could not re-arm the provisioning job; a newer generation owns it")


async def _finish(
    assistant_id: str,
    app_kb_id: str,
    generation: int,
    state: str,
    *,
    reason: Optional[str] = None,
) -> None:
    """Move to a terminal state, which is what removes the work keys.

    ``retain`` is the terminal state for a knowledge base that ends up on managed.
    For born-managed it carries no retention obligation — there are no legacy
    vectors to keep, so ``retainUntil`` is deliberately not set and a future
    reclaim pass finds nothing to reclaim.
    """
    from apis.shared.kb_backend import records as r

    try:
        await _to_thread(
            r.set_migration_state,
            assistant_id,
            app_kb_id,
            state,
            generation,
            None,
            None,
            reason[:1000] if reason else None,
        )
    except Exception as exc:  # noqa: BLE001
        # The work keys survive, so the dispatcher brings this record back and the
        # step re-runs. That is the safe direction: every step here is idempotent,
        # whereas a record wrongly taken out of the queue is never looked at again.
        logger.error(f"kb {app_kb_id}: could not record the terminal state {state}: {exc}")


async def _to_thread(fn, *args):
    """Run a blocking boto3 call off the event loop (Requirement 20.7)."""
    import asyncio

    return await asyncio.to_thread(fn, *args)
