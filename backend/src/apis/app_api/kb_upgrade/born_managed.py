"""Born-managed: stack knowledge-base provisioning onto the FIRST document upload.

``MANAGED_KB_NEW_DEFAULT`` (rollout ladder step 2) makes a new agent's knowledge
base managed from the start. This module is the API-side half — the trigger — and
:mod:`apis.app_api.kb_migration.provisioner` is the worker-side half that does the
minutes-long work.

Why the first upload, and not agent creation
--------------------------------------------
Three reasons, in order of how much they cost if ignored:

1. **Quota.** Managed is one Bedrock knowledge base per agent against a ~10,000
   per-account ceiling. Provisioning at agent creation spends that budget on
   prompt-only agents and abandoned drafts — agents that will never hold a
   document. Provisioning at first upload bounds it to agents that actually use
   RAG.
2. **WYSIWYG.** The agent-creation page uploads documents to the *draft* and lets
   the author test against them on the spot. Provisioning at first upload means
   the engine they test on is the engine they ship.
3. **Nothing to migrate.** A brand-new agent has no corpus, so there is no
   shadow-index-and-verify to run. Trying to reuse the migration for this — which
   an earlier revision of this feature did — writes ``retrievalEngine=managed``
   only at *promotion*, so the very first document is grabbed by the legacy
   pipeline, tested on legacy, and then indexed a second time on managed.

Which is why this declares the engine **up front**: the legacy handler skips a
document only when its record already resolves to ``managed``
(``documents/ingestion/handler._resolve_engine``). Declaring first is what makes
the first document go straight to the managed pipeline instead of both.

What this function must never do
--------------------------------
Fail an upload. Provisioning is an optimisation of *which engine* serves a
knowledge base, never a precondition for storing a document, so every failure
path here is logged and swallowed and leaves the agent on legacy — exactly where
it would have been without this feature. The one thing the caller learns is
whether to label this document ``provisioning``.

It also never blocks. The AWS ``CreateKnowledgeBase`` call is 47–124 s to
``ACTIVE`` and takes minutes end to end; all this does is three conditional
DynamoDB writes and hand the job to the dispatcher's queue.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from apis.app_api.kb_migration.ingestion_consumer import (
    STATUS_PROVISIONING as _STATUS_PROVISIONING,
)
from apis.app_api.kb_upgrade.service import new_default_enabled

logger = logging.getLogger(__name__)

#: The leading ``DOC#`` status a born-managed first upload carries, extending the
#: managed lifecycle to ``provisioning → uploading → complete``.
#:
#: Re-exported from the managed ingestion consumer rather than declared here: the
#: consumer and the provisioning job live in a Lambda image that carries only
#: ``kb_backend`` and ``kb_migration``, so the constant has to be defined on that
#: side of the boundary. Both halves must agree on this string — the trigger writes
#: it, the consumer defers on it, the provisioner clears it.
STATUS_PROVISIONING = _STATUS_PROVISIONING


def is_provisioning_managed(record: Optional[Dict[str, Any]]) -> bool:
    """Whether this record is a managed knowledge base that is not built yet.

    The single definition of "defer to the provisioner", read by the trigger here
    and by the managed ingestion consumer. Both halves must agree: if the consumer
    thought a record was ready while the trigger thought it was provisioning, the
    consumer would dead-letter the first document.

    Tests the engine AND the absence of the AWS identifiers rather than
    ``provisioningState`` alone, because the identifiers are what ingestion
    actually needs. ``provisioningState`` flips to ``active`` in the same write
    that attaches them (``records.attach_aws_ids``), so the two cannot disagree —
    but reading the thing that is used keeps it that way.
    """
    from apis.shared.kb_backend.records import ENGINE_MANAGED, resolve_engine

    if resolve_engine(record) != ENGINE_MANAGED:
        return False
    record = record or {}
    return not (record.get("awsKbId") and record.get("awsDataSourceId"))


async def begin_born_managed(
    assistant_id: str,
    *,
    owner_user_id: str,
    visibility: str = "PRIVATE",
) -> bool:
    """Make sure this agent is heading for a managed knowledge base.

    Returns ``True`` when the document being uploaded should be recorded as
    :data:`STATUS_PROVISIONING` — that is, when the knowledge base is being built
    and this document is waiting on it. ``False`` means "carry on exactly as
    before": either the flag is off, or the knowledge base is already built, or
    something went wrong and the agent stays on legacy.

    Idempotent and safe under concurrency. Two simultaneous first uploads produce
    one knowledge base and one provisioning job; the loser of each conditional
    write falls through to the same answer the winner got.
    """
    if not new_default_enabled():
        return False

    try:
        import asyncio

        from apis.shared.kb_backend import records as r

        # app_kb_id == assistant_id this phase.
        record = await asyncio.to_thread(r.get_kb_record, assistant_id, assistant_id)
        if record is None:
            return await _start(assistant_id, owner_user_id, visibility)
        return await _join(assistant_id, record)
    except Exception as exc:  # noqa: BLE001 — born-managed must never fail an upload
        logger.warning(
            f"kb {assistant_id}: born-managed provisioning could not be started, "
            f"so this upload takes the legacy path: {exc}",
            exc_info=True,
        )
        return False


async def _start(assistant_id: str, owner_user_id: str, visibility: str) -> bool:
    """First upload for an agent that has no KB_Record at all.

    Three conditional writes, in this order and for these reasons:

    1. ``create_provisioning`` — the record must exist before anything claims it,
       and ``attribute_not_exists(PK)`` makes it the arbiter of the concurrent
       first-upload race.
    2. ``adopt_managed_engine`` — declare the engine, so the legacy pipeline skips
       the document that is about to land.
    3. ``set_migration_state(BORN_MANAGED)`` — write the sparse work keys, which is
       what puts the job in the dispatcher's queue. **Last**, because a job picked
       up before step 2 would provision a knowledge base nothing routes to.

    A crash between any two of these is recoverable rather than stranding: see
    :func:`_join`, which is what the next upload runs into.
    """
    import asyncio

    from apis.shared.kb_backend import records as r
    from apis.shared.kb_backend.provisioning import (
        build_client_token,
        new_managed_kb_record,
    )
    from apis.shared.timestamps import utc_now_iso

    fresh = new_managed_kb_record(
        assistant_id,
        owner_user_id,
        # The same deterministic token ``provision_managed_kb`` would build for
        # itself, persisted now so its resume path adopts this record rather than
        # inventing a second token for the same knowledge base.
        client_token=build_client_token(assistant_id, "knowledge-base"),
        visibility=visibility,
    )
    try:
        await asyncio.to_thread(r.create_provisioning, assistant_id, fresh)
    except r.TransitionLost:
        # A concurrent first upload created it between our read and our write.
        # Not an error — re-read and answer from whatever it actually says.
        logger.info(
            f"kb {assistant_id}: record created concurrently during the first upload"
        )
        record = await asyncio.to_thread(r.get_kb_record, assistant_id, assistant_id)
        return await _join(assistant_id, record)

    now = utc_now_iso()
    try:
        await asyncio.to_thread(r.adopt_managed_engine, assistant_id, assistant_id, now)
    except r.TransitionLost:
        # Somebody declared the engine first. Their job owns the provision; this
        # document just waits on it.
        logger.info(f"kb {assistant_id}: engine already declared managed by a concurrent upload")

    await _enqueue(assistant_id, generation=0)
    logger.info(
        f"kb {assistant_id}: born managed — knowledge base queued for provisioning "
        f"on its first document"
    )
    return True


async def _join(assistant_id: str, record: Optional[Dict[str, Any]]) -> bool:
    """An upload arriving while a born-managed provision is (or should be) in flight.

    Covers three situations that look the same from here and must be handled the
    same way:

    * a second document uploaded during the provisioning window;
    * a retried request whose predecessor already did the work;
    * a crash part-way through :func:`_start`, which leaves a managed-intent record
      with no work keys and therefore nothing to finish it.

    The last is why the work keys are re-asserted rather than assumed. Without it a
    single unlucky crash would leave the agent permanently unable to ingest: managed
    intent makes the legacy pipeline skip every upload, and no knowledge base exists
    for the managed one to use.
    """
    if not is_provisioning_managed(record):
        # Either legacy (nothing owed) or a built managed knowledge base (the
        # ordinary managed upload path, byte cap and all).
        return False

    generation = int((record or {}).get("migrationGeneration") or 0)
    state = str((record or {}).get("migrationState") or "")

    from apis.shared.kb_backend import records as r

    if state != r.BORN_MANAGED:
        logger.warning(
            f"kb {assistant_id}: managed intent with no provisioning job "
            f"(migrationState={state!r}); re-queueing it"
        )
        await _enqueue(assistant_id, generation=generation)
    return True


async def _enqueue(assistant_id: str, *, generation: int) -> None:
    """Put the provisioning job in the dispatcher's queue, due immediately.

    ``due_at`` is now rather than later: a person is watching an upload spinner,
    and the dispatcher is rate-bounded anyway. Losing the conditional write means a
    concurrent request queued the same job, which is the desired end state, so it
    is logged at info and not raised.
    """
    import asyncio

    from apis.shared.kb_backend import records as r
    from apis.shared.timestamps import utc_now_iso

    try:
        await asyncio.to_thread(
            r.set_migration_state,
            assistant_id,
            assistant_id,
            r.BORN_MANAGED,
            generation,
            utc_now_iso(),
        )
    except r.TransitionLost:
        logger.info(
            f"kb {assistant_id}: provisioning job already queued by a concurrent request"
        )
