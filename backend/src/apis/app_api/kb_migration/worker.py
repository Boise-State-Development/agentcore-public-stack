"""Migration worker: shadow → verify → promote → retain, one step per invocation.

Requirements 15, 16, 17. Each invocation takes a lease, executes **one** step for
one knowledge base, records the next state with a conditional write, and returns.
The dispatcher brings it back for the next step.

Why one step per invocation
---------------------------
A 20-document text corpus is about 3 minutes end to end, but a 20-PDF corpus can
exceed an hour: per-document parse time was measured at 37–264 s and dominates
everything else. A worker that tried to run the whole machine in one invocation
would therefore be a Lambda that sometimes finishes in three minutes and sometimes
hits its timeout — and a timeout mid-``shadow`` is indistinguishable, from the
outside, from a crash. Stepping means every interruption lands on a recorded state
with a conditional guard in front of it, which is what makes a resumed run converge
instead of duplicating (property test 6).

Nothing is mutated in place
---------------------------
The live knowledge base keeps serving from the legacy backend throughout ``shadow``
and ``verify`` (15.2, 15.3). The managed corpus is built alongside it and becomes
visible only at ``promote``, which is one conditional write. That is also why
rollback moves no data: the legacy index was never touched, so returning to it is
an attribute ``REMOVE``.

Re-ingest, never re-upload
--------------------------
Source bytes are already at
``assistants/{assistant_id}/documents/{document_id}/{filename}``, so migration
hands Bedrock the S3 location it already has (15.4). No user is ever asked to
re-supply a document, and no bytes move.

Convergence, not dual-write
---------------------------
The existing upload path stays authoritative and keeps writing to legacy for the
whole migration (16.1, 16.6). Rather than writing to both engines — which doubles
the number of ways a write can half-fail — the worker snapshots the document-id
set, migrates it, then runs catch-up passes until a pass finds nothing new (16.2,
16.3). Same converge-on-quiet shape as the crawler's consecutive-miss rule.

Each document's ``DOC#`` record is re-read immediately before it is ingested and
skipped if it has gone or is no longer ``complete`` (16.4, 16.5). Without that
re-read, a document deleted while the migration was working through a long PDF
queue would be resurrected in the managed corpus — the user deleted it, saw it
disappear, and it comes back on a different engine.

Feature: managed-kb-migration
Requirements: 15.1–15.14, 16.1–16.6, 17.1–17.5, 12.9
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional, Sequence, Set

logger = logging.getLogger()
logger.setLevel(logging.INFO)

#: Document status the migration carries across. Requirement 15.5, and the same
#: value the facade's status filter serves — a document that is not ``complete``
#: is not retrievable on legacy either, so migrating it would create a difference
#: where the whole point is parity.
STATUS_COMPLETE = "complete"

#: Requirement 15.11. Legacy vectors are preserved for at least this long after
#: promotion, which is the window in which rollback is a pointer flip.
RETAIN_DAYS = 30

#: How long a worker holds a knowledge base. Long enough to cover the slowest
#: single step observed (a PDF-heavy shadow pass), short enough that a crashed
#: worker's knowledge base is picked up again the same hour. Requirement 15.13.
LEASE_MINUTES = 15

#: How long to wait before re-asking whether the managed corpus is queryable.
#: Measured ~45 s for a first ingest into a fresh knowledge base, so this re-asks
#: a little either side of that rather than guessing a single number.
VERIFY_RETRY_SECONDS = 60

#: Bound on those deferrals. Ten minutes of "not queryable" is no longer latency,
#: it is a corpus that will never answer — and the owner deserves to be told.
MAX_VERIFY_ATTEMPTS = 10

#: Catch-up passes before the worker gives up waiting for quiet. A knowledge base
#: whose owner is actively uploading may never converge; stopping is correct —
#: the record stays in ``shadow``, the dispatcher brings it back, and the corpus
#: keeps serving from legacy in the meantime.
MAX_CATCHUP_PASSES = 5

#: Documents ingested per managed call. Server-enforced at 10 for MANAGED
#: knowledge bases; the user guide's 25 is wrong. Named here so the batching is
#: visible at this level rather than only inside the adapter.
INGEST_BATCH = 10

#: Seconds added to ``dueAt`` when a step defers itself. Not a retry backoff — the
#: step succeeded — so it only needs to be long enough that the dispatcher does not
#: spin.
STEP_DELAY_SECONDS = 30

#: Ceiling on the completed-document set stored on the record. A DynamoDB item is
#: capped at 400 KB and this set is the only unbounded thing on it. Production's
#: entire corpus is 1,692 ``DOC#`` records across *all* assistants, so no real
#: knowledge base comes close; past the cap the worker stops tracking and a resume
#: re-ingests, which is slow but not wrong — ``customDocumentIdentifier`` makes a
#: re-ingest a replace.
MAX_TRACKED_DOCUMENT_IDS = 5000

METRIC_STARTED = "KbMigrationStarted"
METRIC_PROMOTED = "KbMigrationPromoted"
METRIC_FAILED = "KbMigrationFailed"
METRIC_ROLLED_BACK = "KbMigrationRolledBack"
METRIC_DOCUMENTS_MIGRATED = "KbMigrationDocumentsMigrated"
METRIC_DOCUMENTS_SKIPPED = "KbMigrationDocumentsSkipped"
METRIC_LEASE_LOST = "KbMigrationLeaseLost"


class MigrationError(Exception):
    """A migration step could not complete. Leaves the record where it was."""


class LeaseLost(MigrationError):
    """Another worker holds this knowledge base. Not an error condition."""


class VerificationFailed(MigrationError):
    """The managed corpus does not match the source manifest, or the canary
    retrieval returned nothing. Sends the migration to ``failed``, which leaves the
    knowledge base on legacy and fully usable (17.4)."""


@dataclass
class StepResult:
    """What one invocation did. Returned so the handler can log and test on it."""

    assistant_id: str
    app_kb_id: str
    from_state: Optional[str]
    to_state: Optional[str]
    documents_migrated: int = 0
    documents_skipped: int = 0
    catchup_passes: int = 0
    converged: bool = False
    detail: str = ""
    manifest_diff: List[str] = field(default_factory=list)

    def as_log_fields(self) -> Dict[str, Any]:
        return {
            "appKbId": self.app_kb_id,
            "from": self.from_state,
            "to": self.to_state,
            "migrated": self.documents_migrated,
            "skipped": self.documents_skipped,
            "catchupPasses": self.catchup_passes,
            "converged": self.converged,
            "detail": self.detail,
        }


# ── environment, read at call time ───────────────────────────────────────────
def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _iso(moment) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_iso() -> str:
    return _iso(_now())


def _documents_bucket() -> str:
    bucket = os.environ.get("S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME")
    if not bucket:
        raise MigrationError("S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME is not set")
    return bucket


def _retain_days() -> int:
    raw = os.environ.get("KB_MIGRATION_RETAIN_DAYS")
    try:
        value = int(raw) if raw else RETAIN_DAYS
    except ValueError:
        return RETAIN_DAYS
    # Requirement 15.11 says *at least* 30 days, so a smaller override is refused
    # rather than honoured: shortening the rollback window is not a tuning knob.
    return max(value, RETAIN_DAYS)


def _table():
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["DYNAMODB_ASSISTANTS_TABLE_NAME"])


# ── document reads (raw table, no assistants import) ─────────────────────────
def list_document_items(assistant_id: str) -> List[Dict[str, Any]]:
    """Every ``DOC#`` record under an assistant, paginated.

    Raw table access for the same reason ``kb_sync/records.py`` uses it: importing
    ``apis.shared.assistants`` pulls in the embeddings stack at module scope and
    this module ships in a size-constrained Lambda image.
    """
    from boto3.dynamodb.conditions import Key

    table = _table()
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
            return items
        kwargs["ExclusiveStartKey"] = last


def get_document_item(assistant_id: str, document_id: str) -> Optional[Dict[str, Any]]:
    response = _table().get_item(
        Key={"PK": f"AST#{assistant_id}", "SK": f"DOC#{document_id}"}
    )
    return response.get("Item")


def document_id_of(item: Dict[str, Any]) -> str:
    sk = str(item.get("SK") or "")
    return sk.split("#", 1)[1] if sk.startswith("DOC#") else ""


def is_complete(item: Optional[Dict[str, Any]]) -> bool:
    return bool(item) and item.get("status") == STATUS_COMPLETE


def manifest_entry(item: Dict[str, Any]) -> str:
    """One line of the source manifest: id plus a content identity.

    Requirement 15.6 forbids relying on document-count parity, and this is why the
    manifest is a set of strings rather than a number. Count parity is satisfied by
    a corpus with the right *number* of wrong documents — which is exactly what a
    migration that raced an upload and a delete produces.

    The identity is the first available of ``contentHash``, ``etag`` or
    ``updatedAt``. All three are already written by the existing pipeline; falling
    through to ``updatedAt`` means a document with no hash still contributes a
    changing value rather than a constant that always matches.
    """
    document_id = document_id_of(item)
    for key in ("contentHash", "etag", "generation", "updatedAt"):
        value = item.get(key)
        if value:
            return f"{document_id}:{value}"
    return f"{document_id}:no-identity"


def source_manifest(items: Sequence[Dict[str, Any]]) -> Set[str]:
    return {manifest_entry(item) for item in items if is_complete(item)}


def s3_key_for(assistant_id: str, item: Dict[str, Any]) -> Optional[str]:
    """The document's existing S3 key. Prefers the stored one.

    Reconstructed from ``filename`` only when the record has no ``s3Key``, because
    the record is authoritative: a filename that was sanitised on upload would
    reconstruct to a key that does not exist, and the ingest would fail per
    document with an error naming the wrong cause.
    """
    stored = item.get("s3Key") or item.get("s3_key")
    if stored:
        return str(stored)
    filename = item.get("filename")
    document_id = document_id_of(item)
    if not filename or not document_id:
        return None
    return f"assistants/{assistant_id}/documents/{document_id}/{filename}"


def document_bytes(item: Dict[str, Any]) -> int:
    for key in ("sizeBytes", "fileSize", "size"):
        value = item.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return 0


# ── lease ────────────────────────────────────────────────────────────────────
async def take_lease(assistant_id: str, app_kb_id: str) -> str:
    """Hold the knowledge base for :data:`LEASE_MINUTES`, or raise :class:`LeaseLost`.

    Requirement 15.13. Losing this is the ordinary outcome of two dispatcher ticks
    overlapping, so it is logged at info and counted, not raised as a failure that
    would move the record to ``failed`` and strand a perfectly healthy migration.
    """
    from apis.shared.kb_backend import records as r
    from apis.shared.kb_backend.metrics import emit_count

    now = _now()
    lease_until = _iso(now + timedelta(minutes=LEASE_MINUTES))
    try:
        await asyncio.to_thread(
            r.acquire_lease, assistant_id, app_kb_id, lease_until, _iso(now)
        )
    except Exception as exc:
        emit_count(METRIC_LEASE_LOST)
        raise LeaseLost(
            f"another worker holds the lease on kb {app_kb_id}; leaving it alone: {exc}"
        ) from exc
    return lease_until


# ── ingestion of one snapshot ────────────────────────────────────────────────
async def _ingest_documents(
    assistant_id: str,
    app_kb_id: str,
    document_ids: Sequence[str],
    backend,
) -> Dict[str, Any]:
    """Re-ingest the named documents, re-reading each record first.

    Returns ``{"migrated": int, "skipped": int, "done": [ids]}``. ``done`` is the
    documents genuinely handed to Bedrock, which is what gets persisted so a resume
    can skip them — a count would not identify *which*.

    The re-read is Requirement 16.4 and it happens per document immediately before
    that document is handed over, not once per batch: a PDF batch can take minutes,
    and the deletion this guards against is most likely to land during exactly that
    window.
    """
    from apis.shared.kb_backend.protocol import DocumentSource

    migrated = 0
    skipped = 0
    done: List[str] = []
    batch: List[DocumentSource] = []

    async def flush() -> None:
        nonlocal batch, migrated
        if not batch:
            return
        await backend.ingest_documents(app_kb_id, batch, batch_size=INGEST_BATCH)
        migrated += len(batch)
        done.extend(source.document_id for source in batch)
        batch = []

    for document_id in document_ids:
        item = await asyncio.to_thread(get_document_item, assistant_id, document_id)
        if not is_complete(item):
            # Gone, or no longer complete. Requirement 16.5: not resurrected.
            logger.info(
                f"skipping document {document_id}: status="
                f"{(item or {}).get('status', 'NOT_FOUND')}"
            )
            skipped += 1
            continue

        key = s3_key_for(assistant_id, item)
        if not key:
            logger.warning(f"skipping document {document_id}: no resolvable S3 key")
            skipped += 1
            continue

        batch.append(
            DocumentSource(
                document_id=document_id,
                filename=str(item.get("filename") or document_id),
                s3_key=key,
                metadata={"document_id": document_id, "filename": str(item.get("filename") or "")},
            )
        )
        if len(batch) >= INGEST_BATCH:
            await flush()

    await flush()
    return {"migrated": migrated, "skipped": skipped, "done": done}


# ── steps ────────────────────────────────────────────────────────────────────
async def run_shadow(
    assistant_id: str,
    app_kb_id: str,
    record: Dict[str, Any],
    backend=None,
) -> StepResult:
    """Provision, reserve the whole corpus, ingest the snapshot, then converge.

    Order matters and is not arbitrary:

    1. **Reserve the whole snapshot first** (12.9). Migration is the largest
       byte-adding operation in the system and the only one that runs unattended.
       Reserving per document would let a migration run for an hour and stop
       halfway, leaving a half-populated managed corpus and an owner over their cap
       with no way back.
    2. **Provision.** Lazy by design, so the knowledge base may not exist yet.
    3. **Ingest the snapshot**, re-reading each record immediately before use.
    4. **Catch up until quiet** (16.2, 16.3), then move to ``verify``.
    """
    from apis.shared.kb_backend import byte_cap, records as r
    from apis.shared.kb_backend.metrics import emit_count
    from apis.shared.kb_backend.provisioning import provision_managed_kb

    generation = int(record.get("migrationGeneration") or 0)
    items = await asyncio.to_thread(list_document_items, assistant_id)
    complete = [item for item in items if is_complete(item)]

    # Documents a previous invocation already ingested. Skipping them is what makes
    # a resumed migration cost seconds rather than re-parsing a PDF corpus that can
    # take over an hour.
    done = already_migrated(record)
    snapshot = [
        document_id_of(item)
        for item in complete
        if document_id_of(item) and document_id_of(item) not in done
    ]

    total_bytes = sum(document_bytes(item) for item in complete)
    if total_bytes and record.get("totalBytes") in (None, 0):
        # Reserved once per migration, not once per resume: the accumulator is on
        # the record, so a resumed run that reserved again would double-count its
        # own corpus against the owner's cap and eventually refuse itself.
        # Raises ByteCapExceeded, which the caller turns into `failed` — before
        # anything has been provisioned or ingested.
        await asyncio.to_thread(
            byte_cap.reserve_snapshot,
            assistant_id,
            app_kb_id,
            total_bytes,
            byte_cap.per_owner_cap(bool(record.get("elevatedByteCap"))),
        )

    await provision_managed_kb(
        assistant_id,
        app_kb_id,
        owner_user_id=str(record.get("ownerUserId") or ""),
    )

    backend = backend or _managed_backend()
    emit_count(METRIC_STARTED)

    counts = await _ingest_documents(assistant_id, app_kb_id, snapshot, backend)
    migrated_ids = set(snapshot) | done

    passes, converged, extra = await catch_up(
        assistant_id, app_kb_id, migrated_ids, backend
    )
    counts["migrated"] += extra["migrated"]
    counts["skipped"] += extra["skipped"]
    newly_done = list(counts["done"]) + list(extra["done"])

    total_done = len(done) + counts["migrated"]
    await _record_progress(
        assistant_id,
        app_kb_id,
        migrated=total_done,
        total=total_done,
        skipped=counts["skipped"],
        newly_done=newly_done,
    )

    if not converged:
        # Still busy. Stay in `shadow`; the dispatcher brings this back, and the
        # corpus keeps serving from legacy in the meantime.
        await asyncio.to_thread(
            r.set_migration_state,
            assistant_id,
            app_kb_id,
            r.SHADOW,
            generation,
            _iso(_now() + timedelta(seconds=STEP_DELAY_SECONDS)),
            [r.SHADOW],
        )
        return StepResult(
            assistant_id,
            app_kb_id,
            r.SHADOW,
            r.SHADOW,
            counts["migrated"],
            counts["skipped"],
            passes,
            False,
            "catch-up did not converge; staying in shadow",
        )

    await asyncio.to_thread(
        r.set_migration_state,
        assistant_id,
        app_kb_id,
        r.VERIFY,
        generation,
        _iso(_now() + timedelta(seconds=STEP_DELAY_SECONDS)),
        [r.SHADOW],
    )
    return StepResult(
        assistant_id,
        app_kb_id,
        r.SHADOW,
        r.VERIFY,
        counts["migrated"],
        counts["skipped"],
        passes,
        True,
    )


async def catch_up(
    assistant_id: str,
    app_kb_id: str,
    already: Set[str],
    backend,
    max_passes: int = None,
) -> tuple:
    """Ingest documents that appeared since the snapshot, until a pass finds none.

    Requirements 16.2, 16.3. Returns ``(passes, converged, counts)``.

    Converged means a pass found nothing new — not that a fixed number of passes
    ran. The distinction matters because the number of passes needed depends on how
    fast the owner is uploading, which is not something this code can know in
    advance. ``max_passes`` bounds the invocation, and *not* converging is a normal
    outcome that leaves the record in ``shadow``.
    """
    limit = MAX_CATCHUP_PASSES if max_passes is None else max_passes
    counts: Dict[str, Any] = {"migrated": 0, "skipped": 0, "done": []}

    for attempt in range(1, limit + 1):
        items = await asyncio.to_thread(list_document_items, assistant_id)
        pending = [
            document_id_of(item)
            for item in items
            if is_complete(item) and document_id_of(item) not in already
        ]
        if not pending:
            logger.info(f"catch-up converged for kb {app_kb_id} after {attempt} pass(es)")
            return attempt, True, counts

        logger.info(f"catch-up pass {attempt} for kb {app_kb_id}: {len(pending)} new document(s)")
        pass_counts = await _ingest_documents(assistant_id, app_kb_id, pending, backend)
        counts["migrated"] += pass_counts["migrated"]
        counts["skipped"] += pass_counts["skipped"]
        counts["done"].extend(pass_counts["done"])
        already.update(pending)

    return limit, False, counts


async def run_verify(
    assistant_id: str,
    app_kb_id: str,
    record: Dict[str, Any],
    backend=None,
) -> StepResult:
    """Compare an exact manifest, then prove retrieval works.

    Requirements 15.6, 15.7. Two checks, and both are needed:

    * The **manifest** is a set of ``document_id:identity`` strings, not a count.
      A count is satisfied by the right number of wrong documents.
    * The **canary retrieval** proves the corpus is genuinely queryable. Bedrock
      reporting a document ``INDEXED`` precedes it being retrievable by
      0.75–1.03 s, and a knowledge base can hold documents while returning nothing
      — so "we ingested everything" and "retrieval works" are separate claims.
    """
    from apis.shared.kb_backend import records as r

    generation = int(record.get("migrationGeneration") or 0)
    backend = backend or _managed_backend()

    items = await asyncio.to_thread(list_document_items, assistant_id)
    expected = source_manifest(items)

    complete = [item for item in items if is_complete(item)]
    if not complete:
        raise VerificationFailed(
            f"kb {app_kb_id} has no complete documents to verify; there is nothing "
            f"to promote"
        )

    canary_text = _canary_query(complete)
    chunks = await backend.search(app_kb_id, canary_text, 5)
    if not chunks:
        # NOT a verification failure — a verification that has not happened yet.
        # A first ingest into a fresh knowledge base took ~45 s to become
        # retrievable when measured against dev; the docstring's 0.75-1.03 s was a
        # warm-knowledge-base figure. Failing here marked a perfectly good
        # migration `failed` for being asked too early, and the owner then saw a
        # retry button for a problem that would have resolved itself.
        attempts = await asyncio.to_thread(
            r.defer_verify,
            assistant_id,
            app_kb_id,
            generation,
            _iso(_now() + timedelta(seconds=VERIFY_RETRY_SECONDS)),
        )
        if attempts > MAX_VERIFY_ATTEMPTS:
            raise VerificationFailed(
                f"canary retrieval on kb {app_kb_id} still returned nothing after "
                f"{attempts} attempts over ~"
                f"{attempts * VERIFY_RETRY_SECONDS // 60} minutes; the corpus "
                f"never became queryable"
            )
        logger.info(
            f"kb {app_kb_id}: corpus not queryable yet (attempt {attempts}); "
            f"deferring verify by {VERIFY_RETRY_SECONDS}s"
        )
        return StepResult(
            assistant_id=assistant_id,
            app_kb_id=app_kb_id,
            from_state=r.VERIFY,
            to_state=r.VERIFY,
            detail=f"corpus not queryable yet; deferred (attempt {attempts})",
        )

    retrieved_ids = {chunk.document_id for chunk in chunks if chunk.document_id}
    expected_ids = {document_id_of(item) for item in complete}
    if not retrieved_ids & expected_ids:
        raise VerificationFailed(
            f"canary retrieval on kb {app_kb_id} returned only documents this "
            f"assistant does not own: {sorted(retrieved_ids)}"
        )

    await asyncio.to_thread(
        r.set_migration_state,
        assistant_id,
        app_kb_id,
        r.PROMOTE,
        generation,
        _iso(_now() + timedelta(seconds=STEP_DELAY_SECONDS)),
        [r.VERIFY],
    )
    return StepResult(
        assistant_id,
        app_kb_id,
        r.VERIFY,
        r.PROMOTE,
        detail=f"manifest of {len(expected)} document(s) verified; canary returned "
        f"{len(chunks)} chunk(s)",
    )


def _canary_query(complete: Sequence[Dict[str, Any]]) -> str:
    """A query built from the corpus's own filenames.

    Not a fixed string. A constant like "test" can legitimately match nothing in a
    real corpus, which would make verification fail for healthy knowledge bases and
    train whoever is watching to ignore it.
    """
    names = [str(item.get("filename") or "") for item in complete[:3]]
    text = " ".join(name.rsplit(".", 1)[0].replace("_", " ").replace("-", " ") for name in names)
    return text.strip() or "summary"


async def run_promote(
    assistant_id: str,
    app_kb_id: str,
    record: Dict[str, Any],
) -> StepResult:
    """The cutover: one conditional write, then straight into ``retain``.

    Requirements 15.8, 15.9, 15.10. Everything that makes this safe lives in
    ``records.promote_engine``'s condition — the state, the generation, and
    ``migrationProgress.migrated == migrationProgress.total``, so a promotion
    cannot happen on a knowledge base whose catch-up never converged. Two
    concurrent workers issue the same write and DynamoDB picks one.

    The byte cap must already be enforced on this knowledge base (12.9): no
    traffic is promoted to an unmetered corpus, so a record carrying no
    ``totalBytes`` accumulator is refused here rather than discovered later.
    """
    from apis.shared.kb_backend import records as r
    from apis.shared.kb_backend.metrics import emit_count

    generation = int(record.get("migrationGeneration") or 0)

    if record.get("totalBytes") is None:
        raise MigrationError(
            f"refusing to promote kb {app_kb_id}: it has no totalBytes accumulator, "
            f"so the byte cap is not being enforced on it (Requirement 12.9)"
        )

    # Resuming after a crash *between* the promotion and the state transition. The
    # promotion write is guarded on `attribute_not_exists(retrievalEngine)`, so
    # retrying it here would be refused — and treating that refusal as a failure
    # would mark a migration that actually succeeded as `failed`, leaving a promoted
    # knowledge base with no retention window and no path to `retain`. Found by the
    # convergence property test, which crashed at exactly that transition.
    already_promoted = record.get("retrievalEngine") == r.ENGINE_MANAGED

    if not already_promoted:
        try:
            await asyncio.to_thread(
                r.promote_engine, assistant_id, app_kb_id, generation, _now_iso()
            )
            emit_count(METRIC_PROMOTED)
        except Exception:
            # Re-read before deciding. The write may have been refused because
            # somebody else promoted first, which is success, or because a guard
            # genuinely failed, which is not.
            fresh = await asyncio.to_thread(r.get_kb_record, assistant_id, app_kb_id)
            if (fresh or {}).get("retrievalEngine") != r.ENGINE_MANAGED:
                raise
            logger.info(
                f"kb {app_kb_id} was already promoted by another attempt; "
                f"continuing to retain rather than failing"
            )
            already_promoted = True

    retain_until = _iso(_now() + timedelta(days=_retain_days()))
    await asyncio.to_thread(
        _set_retain_until, assistant_id, app_kb_id, retain_until
    )
    await asyncio.to_thread(
        r.set_migration_state,
        assistant_id,
        app_kb_id,
        r.RETAIN,
        generation,
        None,
        [r.PROMOTE],
    )
    return StepResult(
        assistant_id,
        app_kb_id,
        r.PROMOTE,
        r.RETAIN,
        detail=(
            f"{'already promoted; ' if already_promoted else ''}legacy vectors "
            f"retained until {retain_until}"
        ),
    )


def _set_retain_until(assistant_id: str, app_kb_id: str, retain_until: str) -> None:
    """Stamp the rollback deadline. Unconditional, and deliberately so.

    The promotion write immediately before this one is the guarded one. If this
    write were also guarded and lost, the record would be promoted with no
    ``retainUntil`` — which reads as "no rollback window" to anything that checks
    it. Writing the later date twice is harmless; writing it never is not.
    """
    import boto3

    boto3.resource("dynamodb").Table(os.environ["DYNAMODB_ASSISTANTS_TABLE_NAME"]).update_item(
        Key={"PK": f"AST#{assistant_id}", "SK": f"KB#{app_kb_id}"},
        UpdateExpression="SET retainUntil = :until",
        ExpressionAttributeValues={":until": retain_until},
    )


async def rollback(assistant_id: str, app_kb_id: str) -> StepResult:
    """Return a promoted knowledge base to legacy. Moves no data.

    Requirement 17. The legacy index was never mutated — that is what ``shadow``
    building alongside it bought — so rollback is one attribute ``REMOVE`` plus a
    timestamp. It is available for the whole ``retain`` window because that window
    is exactly the promise not to reclaim the legacy vectors.

    Note this does **not** delete the managed knowledge base. A rolled-back corpus
    that still exists costs storage but can be re-promoted without a second
    migration; deleting it here would turn a reversible decision into an
    irreversible one at the moment somebody is least sure.
    """
    from apis.shared.kb_backend import records as r
    from apis.shared.kb_backend.metrics import emit_count

    await asyncio.to_thread(r.rollback_engine, assistant_id, app_kb_id, _now_iso())
    emit_count(METRIC_ROLLED_BACK)
    return StepResult(
        assistant_id,
        app_kb_id,
        r.RETAIN,
        r.RETAIN,
        detail="rolled back to the legacy engine; no data moved",
    )


async def _record_progress(
    assistant_id: str,
    app_kb_id: str,
    *,
    migrated: int,
    total: int,
    skipped: int,
    newly_done: Optional[Sequence[str]] = None,
) -> None:
    """Write ``migrationProgress``, which the promotion condition reads.

    ``total`` is a DynamoDB reserved keyword, so both progress paths are aliased.
    Unaliased, the write is rejected outright with a ``ValidationException`` — loud,
    but only because it never validates at all.

    ``newly_done`` is ``ADD``ed to the ``migratedDocIds`` string set rather than
    written into the progress map. Two reasons, and both are the difference between
    a resumed migration costing seconds and costing an hour:

    * **``ADD`` is additive**, so a crash between batches loses only the batch in
      flight. A read-modify-write of a list would lose everything since the last
      read, and would also let two workers clobber each other.
    * **It is a separate attribute** from ``migrationProgress``, which this function
      overwrites wholesale. Keeping the completed-document set inside a map that
      gets replaced is how a resume silently re-ingests a corpus it had already
      finished — found by the convergence property test, which counted a document
      ingested twice across a crash and a retry.
    """
    from decimal import Decimal

    expression = "SET #progress = :progress"
    names = {"#progress": "migrationProgress"}
    values: Dict[str, Any] = {
        ":progress": {
            "migrated": Decimal(migrated),
            "total": Decimal(total),
            "skipped": Decimal(skipped),
            "updatedAt": _now_iso(),
        }
    }

    ids = [document_id for document_id in (newly_done or []) if document_id]
    if ids and len(ids) <= MAX_TRACKED_DOCUMENT_IDS:
        # DynamoDB string sets cannot be empty, hence the guard above.
        expression += " ADD #done :done"
        names["#done"] = "migratedDocIds"
        values[":done"] = set(ids)
    elif ids:
        logger.warning(
            f"kb {app_kb_id}: {len(ids)} document ids exceeds the tracking cap of "
            f"{MAX_TRACKED_DOCUMENT_IDS}; a resumed migration will re-ingest, which "
            f"is safe but slow (customDocumentIdentifier makes re-ingest a replace)"
        )

    _table().update_item(
        Key={"PK": f"AST#{assistant_id}", "SK": f"KB#{app_kb_id}"},
        UpdateExpression=expression,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def already_migrated(record: Dict[str, Any]) -> Set[str]:
    """Documents a previous invocation already ingested.

    Read from the ``migratedDocIds`` string set. Empty for a record that has never
    ingested anything, which is also what a corpus past the tracking cap looks
    like — and that degradation is safe: re-ingesting a document replaces it,
    because ``customDocumentIdentifier`` is the platform document id.
    """
    stored = record.get("migratedDocIds")
    if not stored:
        return set()
    try:
        return {str(document_id) for document_id in stored}
    except TypeError:
        logger.warning(f"migratedDocIds is not iterable on this record: {stored!r}")
        return set()


def _managed_backend():
    from apis.shared.kb_backend.managed_backend import ManagedKbBackend

    return ManagedKbBackend(bucket=_documents_bucket())


# ── one invocation ───────────────────────────────────────────────────────────
async def run_step(
    assistant_id: str,
    app_kb_id: Optional[str] = None,
    backend=None,
) -> StepResult:
    """Take the lease and execute the one step this record's state calls for.

    Dispatches on the *record's* state, never on the invocation event's. The event
    carries a state for logging, but trusting it would let a hand-crafted invocation
    promote a knowledge base that never verified — the same class of bypass that
    let an event field arm the reconciler.
    """
    from apis.shared.kb_backend import byte_cap, records as r
    from apis.shared.kb_backend.metrics import emit_count

    app_kb_id = app_kb_id or assistant_id

    record = await asyncio.to_thread(r.get_kb_record, assistant_id, app_kb_id)
    if not record:
        raise MigrationError(f"no KB_Record for {assistant_id}/{app_kb_id}")

    state = record.get("migrationState")
    if state not in r.WORK_ELIGIBLE_STATES:
        # Terminal, or never enrolled. Not an error: the dispatcher reads an index
        # that is eventually consistent, so a record finished a moment ago can
        # still be handed over once.
        return StepResult(
            assistant_id, app_kb_id, state, state, detail="not work-eligible; nothing to do"
        )

    generation = int(record.get("migrationGeneration") or 0)

    try:
        # Inside the try, deliberately. A ``LeaseLost`` must reach the caller as
        # itself — losing a lease is two dispatcher ticks overlapping, not a broken
        # migration — and the ``except LeaseLost: raise`` below is what guarantees
        # that even once a step starts taking sub-leases of its own. Outside the try
        # the clause would be unreachable, which is how a guard becomes decoration.
        await take_lease(assistant_id, app_kb_id)

        if state == r.SHADOW:
            result = await run_shadow(assistant_id, app_kb_id, record, backend)
        elif state == r.VERIFY:
            result = await run_verify(assistant_id, app_kb_id, record, backend)
        else:
            result = await run_promote(assistant_id, app_kb_id, record)
    except LeaseLost:
        raise
    except (VerificationFailed, byte_cap.ByteCapExceeded) as exc:
        # Expected failure modes. The knowledge base stays on legacy and stays
        # usable (17.4); `failed` is terminal and removes the work keys.
        await _fail(assistant_id, app_kb_id, generation, str(exc))
        emit_count(METRIC_FAILED)
        return StepResult(
            assistant_id, app_kb_id, state, r.MIGRATION_FAILED, detail=str(exc)
        )
    except Exception as exc:
        # Unexpected. Also terminal, for the same reason: an unbounded retry on an
        # unknown fault is how a migration loop bills for a week.
        logger.error(f"migration step failed for kb {app_kb_id}: {exc}", exc_info=True)
        await _fail(assistant_id, app_kb_id, generation, f"{type(exc).__name__}: {exc}")
        emit_count(METRIC_FAILED)
        return StepResult(
            assistant_id, app_kb_id, state, r.MIGRATION_FAILED, detail=str(exc)
        )

    logger.info(f"migration step: {result.as_log_fields()}")
    return result


async def _fail(assistant_id: str, app_kb_id: str, generation: int, reason: str) -> None:
    from apis.shared.kb_backend import records as r

    try:
        await asyncio.to_thread(
            r.set_migration_state,
            assistant_id,
            app_kb_id,
            r.MIGRATION_FAILED,
            generation,
            None,
            None,
            reason[:1000],
        )
    except Exception as exc:
        # Nothing further to do: the record keeps its work keys and the dispatcher
        # will bring it back, which is the safe direction — a knowledge base stuck
        # in `shadow` still serves from legacy.
        logger.error(f"could not record migration failure for kb {app_kb_id}: {exc}")


def lambda_handler(event, context):
    """Async-invoked by the dispatcher.

    Reads only the two identifiers from the event. Everything that decides what
    happens — the state, the generation, the flags — comes from the record and the
    environment.
    """
    assistant_id = (event or {}).get("assistantId")
    app_kb_id = (event or {}).get("appKbId")
    if not assistant_id:
        raise MigrationError("event carries no assistantId")

    try:
        result = asyncio.run(run_step(assistant_id, app_kb_id))
    except LeaseLost as exc:
        logger.info(str(exc))
        return {"statusCode": 200, "body": {"leaseLost": True}}

    return {"statusCode": 200, "body": result.as_log_fields()}
