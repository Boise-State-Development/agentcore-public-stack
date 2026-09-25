"""Tear down a deleted agent's managed knowledge base: the worker's ``teardown`` step.

Deleting an agent (``DELETE /assistants/{id}``) or purging a project (its harness)
removes every row in the agent's partition except the ``KB#`` record, which it
queues here instead (:func:`queue_teardown`, app-api's half of this module).
Before this step existed, that record and the Bedrock knowledge base it points at
were simply left behind: the knowledge base stayed ``ACTIVE`` and billed, and
nothing would ever remove it, because the reconciler never removes a record by
design and ships disarmed.

The delete runs here, not in app-api, because this worker holds the provisioning
grant (``bedrock:DeleteKnowledgeBase``/``DeleteDataSource``/``ListKnowledgeBases``)
and app-api is deliberately never given it. It runs under the worker lease the
dispatcher hands out, which gives it the two properties it needs for free: it
cannot start while a provisioning or migration step still holds the knowledge
base, and a teardown killed mid-poll is handed back until it finishes.

What one step does, in order:

1. Find the knowledge base: ``awsKbId`` on the record, or, for a record that was
   mid-provisioning when its agent was deleted, a knowledge base carrying the name
   provisioning gives it (the create can land before its id is recorded).
2. Delete it through the tombstoned saga (``tombstones.delete_knowledge_base``):
   tombstone first, the data source (best effort), the knowledge base, poll until
   AWS stops listing it, clear the tombstone, and only then remove the ``KB#``
   record. With no knowledge base to delete, the record is simply removed.
3. Clear any per-document tombstones: those documents went with the knowledge base.

Every failure re-queues the record rather than failing it. ``failed`` is terminal,
and a teardown that stops trying is exactly the silent bill this exists to end.
``DELETE_UNSUCCESSFUL`` is re-checked daily rather than every tick, because it is an
operator state that waiting does not clear.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: When a teardown that did not finish is handed back. Long enough that a
#: knowledge base still ``DELETING`` has usually gone by then.
RETRY_SECONDS = 15 * 60

#: ``DELETE_UNSUCCESSFUL`` does not clear by waiting (Requirement 13.7). Look again
#: daily, so the record stays visible as work without spinning the queue.
DELETE_UNSUCCESSFUL_RETRY_SECONDS = 24 * 60 * 60

METRIC_TORN_DOWN = "KbTeardownCompleted"
METRIC_DEFERRED = "KbTeardownDeferred"


async def queue_teardown(assistant_id: str) -> bool:
    """Queue the agent's managed knowledge base for teardown. App-api's half.

    Called by both agent delete paths before they destroy anything, so a failure
    here fails the delete while it is still retryable. ``True`` when there was a
    ``KB#`` record to queue; ``False`` for an agent that never had one (every
    legacy agent) or a retried delete whose teardown already finished.
    """
    from apis.shared.kb_backend import records as r
    from apis.shared.timestamps import utc_now_iso

    # app_kb_id == assistant_id this phase.
    record = await asyncio.to_thread(r.request_teardown, assistant_id, assistant_id, utc_now_iso())
    if record is None:
        return False
    logger.info(
        f"kb {assistant_id}: queued for teardown (awsKbId={record.get('awsKbId')}, "
        f"migrationState={record.get('migrationState')})"
    )
    return True


def _later(seconds: int) -> str:
    from datetime import timedelta

    from apis.app_api.kb_migration.worker import _iso, _now

    return _iso(_now() + timedelta(seconds=seconds))


def find_knowledge_base_by_name(client, app_kb_id: str) -> Optional[str]:
    """The id of the knowledge base provisioning would have named for ``app_kb_id``.

    Only asked for a record that has a ``clientToken`` (a create may have been
    issued) and no ``awsKbId`` (its id was never recorded). Names are unique per
    account and derived from ``app_kb_id``, so a match can only be this record's
    own knowledge base.
    """
    from apis.shared.kb_backend.provisioning import _resource_name
    from apis.shared.kb_backend.tombstones import iter_knowledge_base_summaries

    name = _resource_name(app_kb_id)
    for summary in iter_knowledge_base_summaries(client):
        if summary.get("name") == name and summary.get("knowledgeBaseId"):
            return summary["knowledgeBaseId"]
    return None


def clear_document_tombstones(assistant_id: str, app_kb_id: str) -> int:
    """Remove the per-document tombstones of a knowledge base AWS confirmed gone."""
    from apis.shared.kb_backend import tombstones as tb
    from apis.shared.kb_backend.records import document_tombstone_sk

    prefix = document_tombstone_sk(app_kb_id, "")
    cleared = 0
    for item in tb.iter_tombstones(assistant_id):
        if str(item.get("SK", "")).startswith(prefix) and item.get("documentId"):
            tb.clear_document_tombstone(assistant_id, app_kb_id, item["documentId"], True)
            cleared += 1
    return cleared


def _delete(assistant_id: str, app_kb_id: str, record: Dict[str, Any], client) -> Optional[str]:
    """Steps 1-3, blocking. Returns the AWS id deleted, or ``None`` if there was none."""
    from apis.shared.kb_backend import tombstones as tb

    aws_kb_id = record.get("awsKbId")
    if not aws_kb_id and record.get("clientToken"):
        aws_kb_id = find_knowledge_base_by_name(client, app_kb_id)

    if aws_kb_id:
        tb.delete_knowledge_base(
            assistant_id,
            app_kb_id,
            aws_kb_id,
            record.get("awsDataSourceId"),
            client=client,
            remove_record=True,
            delete_data_source=True,
        )
    else:
        # Nothing in AWS: a legacy record, or a provisioning that never created
        # anything. Confirmed absent in the only sense available, so the record goes.
        tb.remove_kb_record(assistant_id, app_kb_id, True)

    clear_document_tombstones(assistant_id, app_kb_id)
    return aws_kb_id


async def run_teardown(
    assistant_id: str,
    app_kb_id: str,
    record: Dict[str, Any],
    client=None,
):
    """Delete the knowledge base and its record. Called under the worker lease."""
    from apis.app_api.kb_migration.worker import StepResult
    from apis.shared.kb_backend import records as r
    from apis.shared.kb_backend import tombstones as tb
    from apis.shared.kb_backend.managed_backend import bedrock_agent_client
    from apis.shared.kb_backend.metrics import emit_count

    generation = int(record.get("migrationGeneration") or 0)
    try:
        client = client or bedrock_agent_client()
        aws_kb_id = await asyncio.to_thread(
            functools.partial(_delete, assistant_id, app_kb_id, record, client)
        )
    except Exception as exc:  # noqa: BLE001 - every failure re-queues; see the module docstring
        unsuccessful = isinstance(exc, tb.DeleteUnsuccessful)
        delay = DELETE_UNSUCCESSFUL_RETRY_SECONDS if unsuccessful else RETRY_SECONDS
        log = logger.error if unsuccessful else logger.warning
        log(f"teardown of kb {app_kb_id} did not finish; re-queued in {delay}s: {exc}")
        try:
            await asyncio.to_thread(
                r.defer_teardown, assistant_id, app_kb_id, generation, _later(delay), str(exc)
            )
        except r.TransitionLost:
            logger.info(f"kb {app_kb_id}: teardown record changed underneath; not re-queued")
        emit_count(METRIC_DEFERRED)
        return StepResult(
            assistant_id, app_kb_id, r.TEARDOWN, r.TEARDOWN,
            detail=f"teardown deferred: {exc}",
        )

    emit_count(METRIC_TORN_DOWN)
    detail = f"deleted knowledge base {aws_kb_id}" if aws_kb_id else "no knowledge base in AWS"
    logger.info(f"kb {app_kb_id}: torn down ({detail}); record removed")
    return StepResult(
        assistant_id, app_kb_id, r.TEARDOWN, None, converged=True, detail=detail
    )


__all__ = [
    "DELETE_UNSUCCESSFUL_RETRY_SECONDS",
    "METRIC_DEFERRED",
    "METRIC_TORN_DOWN",
    "RETRY_SECONDS",
    "clear_document_tombstones",
    "find_knowledge_base_by_name",
    "queue_teardown",
    "run_teardown",
]
