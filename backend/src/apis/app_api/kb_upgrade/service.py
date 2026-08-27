"""Enrolment and status for the owner-facing knowledge base upgrade.

Three things happen here and nothing else: derive what the card should show,
move a record into ``shadow``, and dismiss the one-time success notice. Promotion
belongs to the worker, after verification — this module never writes
``retrievalEngine``, so no HTTP request can put a knowledge base on the managed
backend without the corpus having been carried across and checked first.

Enrolment is deliberately two conditional writes rather than one put:

1. ``create_provisioning`` — guarded on ``attribute_not_exists(PK)``.
2. ``set_migration_state(SHADOW, ...)`` — guarded on the generation.

A single ``put_item`` with ``migrationState="shadow"`` baked in would look
simpler and would be **wrong**: ``KbRecord.to_item`` does not write the
``GSI7_PK``/``GSI7_SK`` work keys, which only ``set_migration_state`` maintains.
The record would exist, claim to be migrating, and be invisible to the
dispatcher's sparse-index sweep forever.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from apis.app_api.kb_upgrade.models import (
    DocumentNotCarried,
    EnrollResponse,
    UpgradeProgress,
    UpgradeStatusResponse,
)

logger = logging.getLogger(__name__)


class UpgradeUnavailable(Exception):
    """The upgrade cannot be offered right now. Carries user-safe copy."""


#: Gate on the same flag the dispatcher reads. If the worker cannot run, offering
#: the upgrade would park a record in ``shadow`` that nothing ever picks up — a
#: spinner with no engine behind it. Requirement 23.1 says show nothing when no
#: action is available, and "available" has to mean actionable.
FLAG_MIGRATION_ENABLED = "MANAGED_KB_MIGRATION_ENABLED"

#: Affirmative spellings, matching ``dispatcher._TRUTHY`` exactly. An allow-list
#: rather than truthiness, because the value being designed around is present but
#: empty: ``bool("")`` is right by luck and ``bool("false")`` is not.
_TRUTHY = frozenset({"1", "true", "yes", "on", "enabled"})

#: How soon the dispatcher may pick up a freshly enrolled record. Now, not later:
#: the user just asked for it, and the dispatcher is already rate-bounded.
_DUE_IMMEDIATELY = timedelta(0)

STATUS_COMPLETE = "complete"


def migration_enabled() -> bool:
    """Whether the upgrade may be offered at all.

    Read at call time, never bound as a default argument — a module-level default
    is captured at import and makes the flag unpatchable, which already cost this
    feature a 33-second test that ignored its own override.
    """
    return (os.environ.get(FLAG_MIGRATION_ENABLED) or "").strip().lower() in _TRUTHY


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


# ── Document classification (Requirement 21) ─────────────────────────────────
def _extension_of(filename: str) -> str:
    _, _, tail = str(filename or "").rpartition(".")
    return f".{tail.lower()}" if tail else ""


def _supported_extensions() -> frozenset:
    """The ingestion pipeline's own extension set, read from its module.

    Imported here rather than copied. A copied list is exactly the drift that
    produced the tag-contract defect: three files agreeing only because they all
    fell back to the same hardcoded default. ``docling_processor``'s module scope
    is stdlib-only, so this costs nothing at request time.
    """
    from apis.app_api.documents.ingestion.processors.docling_processor import (
        DOCLING_SUPPORTED_EXTENSIONS,
    )

    return frozenset(DOCLING_SUPPORTED_EXTENSIONS)


def classify_document(item: Dict[str, Any]) -> Optional[DocumentNotCarried]:
    """Describe why a document will not be carried across, or ``None`` if it will.

    Requirement 21.4 turns on the ``unsupported_format`` / ``processing_failure``
    split: the two demand different actions from the user. Telling someone to
    "retry" a ``.pages`` file wastes a minute and teaches them the retry button
    does not work.

    Note ``deleting`` is reported rather than hidden. The ordinary document list
    filters that status out as soft-deleted, which is right there and wrong here:
    101 of the 200 affected production records are stuck in it, and a user who is
    never shown them cannot tell that they are stuck.
    """
    status = str(item.get("status") or "").strip()
    if status == STATUS_COMPLETE:
        return None

    document_id = str(item.get("documentId") or "")
    if not document_id:
        sk = str(item.get("SK") or "")
        document_id = sk.split("#", 1)[1] if sk.startswith("DOC#") else ""
    filename = str(item.get("filename") or "(unnamed file)")
    extension = _extension_of(filename)
    unsupported = bool(extension) and extension not in _supported_extensions()

    if status == "failed" and unsupported:
        return DocumentNotCarried(
            documentId=document_id,
            filename=filename,
            status=status,
            kind="unsupported_format",
            message=(
                f"This platform cannot read {extension} files, so this document was "
                "never added to your knowledge base. Save it as a PDF or Word "
                "document and upload it again."
            ),
            retryable=False,
        )
    if status == "failed":
        stored = str(item.get("errorMessage") or "").strip()
        detail = f" The reason given was: {stored}" if stored else ""
        return DocumentNotCarried(
            documentId=document_id,
            filename=filename,
            status=status,
            kind="processing_failure",
            message=(
                "This document could not be processed, so it is not in your "
                f"knowledge base and the upgrade cannot carry it across.{detail}"
            ),
            retryable=True,
        )
    if status == "deleting":
        return DocumentNotCarried(
            documentId=document_id,
            filename=filename,
            status=status,
            kind="being_removed",
            message=(
                "This document is part-way through being removed. It will not be "
                "carried across. If you still want it, upload it again once the "
                "removal finishes."
            ),
            retryable=False,
        )
    return DocumentNotCarried(
        documentId=document_id,
        filename=filename,
        status=status,
        kind="still_processing",
        message=(
            "This document is still being processed. Documents that are not "
            "finished when the upgrade starts will not be carried across."
        ),
        retryable=True,
    )


def _document_items(assistant_id: str) -> List[Dict[str, Any]]:
    """Every ``DOC#`` item under an assistant, unfiltered.

    Raw query rather than ``list_assistant_documents``, on purpose and for two
    reasons: that function drops ``deleting`` documents, which are the single
    largest group Requirement 21 exists to surface, and it auto-fails stale ones
    as a side effect — a write triggered by rendering a card.
    """
    import boto3
    from boto3.dynamodb.conditions import Key

    table_name = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not table_name:
        # Fail closed and loudly enough to see, but do not take the card down: a
        # misconfigured table name must not make an upgradeable KB look clean.
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME is not set")

    table = boto3.resource("dynamodb").Table(table_name)
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


def _partition_documents(
    items: List[Dict[str, Any]],
) -> Tuple[int, List[DocumentNotCarried]]:
    """Split into (count carried, descriptions of those not carried)."""
    carried = 0
    stranded: List[DocumentNotCarried] = []
    for item in items:
        issue = classify_document(item)
        if issue is None:
            carried += 1
        else:
            stranded.append(issue)
    return carried, stranded


# ── Status ───────────────────────────────────────────────────────────────────
def _progress_of(record: Dict[str, Any]) -> Optional[UpgradeProgress]:
    stored = record.get("migrationProgress") or {}
    if not stored:
        return None
    return UpgradeProgress(
        completed=int(stored.get("migrated") or 0),
        total=int(stored.get("total") or 0),
        skipped=int(stored.get("skipped") or 0),
    )


#: Failure reasons in the user's language. The stored ``migrationError`` is
#: written for an operator; rendering it raw is how a user ends up reading
#: "ByteCapExceeded".
_FAILURE_COPY = {
    "ByteCapExceeded": (
        "Your knowledge base is larger than the current upgrade size limit, so "
        "the upgrade stopped before changing anything."
    ),
    "VerificationFailed": (
        "The upgraded copy did not return the same results as your current one, "
        "so it was discarded rather than switched over."
    ),
}

_FAILURE_FALLBACK = (
    "Something went wrong part-way through the upgrade, so it was stopped and "
    "nothing was changed."
)


def _failure_reason(record: Dict[str, Any]) -> str:
    stored = str(record.get("migrationError") or "")
    for token, copy in _FAILURE_COPY.items():
        if token in stored:
            return copy
    return _FAILURE_FALLBACK


async def get_upgrade_status(
    assistant_id: str,
    *,
    can_edit: bool,
) -> UpgradeStatusResponse:
    """Derive everything the card renders.

    ``can_edit`` only ever *removes* the control (Requirement 23.7). A viewer
    still gets an honest phase — they may legitimately see that an upgrade is
    running — but never ``canUpgrade``.
    """
    import asyncio

    from apis.shared.kb_backend import records as r

    record = await asyncio.to_thread(r.get_kb_record, assistant_id, assistant_id)
    state = str((record or {}).get("migrationState") or "")

    if record and r.resolve_engine(record) == r.ENGINE_MANAGED:
        # Already upgraded. The only thing owed is the one-time notice, and only
        # until it is dismissed — never a permanent badge (Requirement 23.4).
        pending = not record.get("upgradeNoticeDismissedAt")
        return UpgradeStatusResponse(
            phase="succeeded",
            canUpgrade=False,
            noticePending=bool(pending and can_edit),
            progress=_progress_of(record),
        )

    if state in (r.SHADOW, r.VERIFY, r.PROMOTE):
        return UpgradeStatusResponse(
            phase="in_progress",
            canUpgrade=False,
            progress=_progress_of(record or {}),
        )

    if state == r.MIGRATION_FAILED:
        # Still on the legacy backend, which keeps working. Retry is offered to
        # editors; the phase itself is not hidden (Requirement 23.5).
        return UpgradeStatusResponse(
            phase="failed",
            canUpgrade=can_edit,
            reason=_failure_reason(record or {}),
            progress=_progress_of(record or {}),
        )

    if not (can_edit and migration_enabled()):
        return UpgradeStatusResponse(phase="none", canUpgrade=False)

    items = await asyncio.to_thread(_document_items, assistant_id)
    if not items:
        # An empty knowledge base has nothing to carry across, so there is no
        # action to take and therefore nothing to show (Requirement 23.1).
        return UpgradeStatusResponse(phase="none", canUpgrade=False)

    carried, stranded = _partition_documents(items)
    if not carried:
        # Every document is already stranded. Offering an upgrade that would
        # carry nothing is worse than useless, but the stranded list is exactly
        # what this owner needs to see (Requirement 21.3).
        return UpgradeStatusResponse(
            phase="none",
            canUpgrade=False,
            documentsNotCarried=stranded,
        )

    return UpgradeStatusResponse(
        phase="available",
        canUpgrade=True,
        progress=UpgradeProgress(completed=0, total=carried, skipped=len(stranded)),
        documentsNotCarried=stranded,
    )


# ── Enrolment ────────────────────────────────────────────────────────────────
async def enroll(
    assistant_id: str,
    *,
    owner_user_id: str,
    visibility: str = "PRIVATE",
) -> EnrollResponse:
    """Move this knowledge base into ``shadow``, or report one already running.

    Idempotent by construction: both writes are conditional, so a double-click
    produces one migration and one "already running" answer rather than two
    provisioning sagas racing over the same corpus.
    """
    import asyncio

    from apis.shared.kb_backend import records as r

    if not migration_enabled():
        raise UpgradeUnavailable(
            "Upgrades are not being accepted at the moment. Nothing has changed."
        )

    record = await asyncio.to_thread(r.get_kb_record, assistant_id, assistant_id)

    if record and r.resolve_engine(record) == r.ENGINE_MANAGED:
        return EnrollResponse(
            phase="succeeded",
            started=False,
            message="This knowledge base has already been upgraded.",
        )

    state = str((record or {}).get("migrationState") or "")
    if state in (r.SHADOW, r.VERIFY, r.PROMOTE):
        return EnrollResponse(
            phase="in_progress",
            started=False,
            message="The upgrade is already running.",
        )

    generation = int((record or {}).get("migrationGeneration") or 0)

    if record is None:
        # Zero-backfill: legacy knowledge bases have no KB record at all, so
        # enrolment is where the record first comes into existence.
        fresh = r.KbRecord(
            app_kb_id=assistant_id,
            owner_user_id=owner_user_id,
            visibility=visibility,
            provisioning_state=r.PROVISIONING,
        )
        try:
            await asyncio.to_thread(r.create_provisioning, assistant_id, fresh)
        except r.TransitionLost:
            # Another request created it between our read and our write. Not an
            # error: fall through and let the state transition arbitrate.
            logger.info(
                f"kb {assistant_id}: record created concurrently during enrolment"
            )
            record = await asyncio.to_thread(r.get_kb_record, assistant_id, assistant_id)
            generation = int((record or {}).get("migrationGeneration") or 0)

    due_at = _iso(_now() + _DUE_IMMEDIATELY)
    try:
        await asyncio.to_thread(
            r.set_migration_state,
            assistant_id,
            assistant_id,
            r.SHADOW,
            generation,
            due_at=due_at,
        )
    except r.TransitionLost:
        return EnrollResponse(
            phase="in_progress",
            started=False,
            message="The upgrade is already running.",
        )

    logger.info(f"kb {assistant_id}: enrolled into shadow at generation {generation}")
    return EnrollResponse(
        phase="in_progress",
        started=True,
        message=(
            "Upgrade started. Your knowledge base keeps working while it runs, "
            "and you can leave this page."
        ),
    )


async def retry(assistant_id: str, *, owner_user_id: str) -> EnrollResponse:
    """Re-enter ``shadow`` from ``failed``, on a fresh generation.

    The generation bump is what makes the retry safe: every conditional write
    belonging to the abandoned attempt is guarded on the old generation, so a
    straggler worker from the failed run cannot land a write on the new one.
    """
    import asyncio

    from apis.shared.kb_backend import records as r

    if not migration_enabled():
        raise UpgradeUnavailable(
            "Upgrades are not being accepted at the moment. Nothing has changed."
        )

    record = await asyncio.to_thread(r.get_kb_record, assistant_id, assistant_id)
    if record is None:
        return await enroll(assistant_id, owner_user_id=owner_user_id)

    state = str(record.get("migrationState") or "")
    if state != r.MIGRATION_FAILED:
        # Nothing to retry. Report the truth rather than starting a second run.
        if state in (r.SHADOW, r.VERIFY, r.PROMOTE):
            return EnrollResponse(
                phase="in_progress",
                started=False,
                message="The upgrade is already running.",
            )
        if r.resolve_engine(record) == r.ENGINE_MANAGED:
            return EnrollResponse(
                phase="succeeded",
                started=False,
                message="This knowledge base has already been upgraded.",
            )
        return await enroll(
            assistant_id,
            owner_user_id=owner_user_id,
            visibility=str(record.get("visibility") or "PRIVATE"),
        )

    generation = int(record.get("migrationGeneration") or 0)
    try:
        await asyncio.to_thread(
            r.retry_from_failed,
            assistant_id,
            assistant_id,
            generation,
            _iso(_now() + _DUE_IMMEDIATELY),
        )
    except r.TransitionLost:
        # A concurrent retry got there first. Its attempt is running, so this is
        # a success from the user's point of view.
        return EnrollResponse(
            phase="in_progress",
            started=False,
            message="The upgrade is already running.",
        )
    logger.info(f"kb {assistant_id}: retried into generation {generation + 1}")
    return EnrollResponse(
        phase="in_progress",
        started=True,
        message=(
            "Upgrade restarted. Your knowledge base keeps working while it runs."
        ),
    )


async def dismiss_notice(assistant_id: str) -> None:
    """Retire the one-time success notice (Requirement 23.4)."""
    import asyncio

    from apis.shared.kb_backend import records as r

    try:
        await asyncio.to_thread(
            r.dismiss_upgrade_notice, assistant_id, assistant_id, _iso(_now())
        )
    except r.TransitionLost:
        # No record, so no notice to dismiss. Nothing owed, nothing to report.
        logger.info(f"kb {assistant_id}: notice dismissal for a record that is absent")
