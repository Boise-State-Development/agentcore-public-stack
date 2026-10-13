"""Re-index a conversation's evicted large documents when it is opened.

Large-document spec §4E. A document in the shared ``attachments`` knowledge base
is evicted after ``ATTACHMENT_KB_RETENTION_DAYS`` without a turn in its
conversation (its sidecars and Tier 1 reads are untouched). Ingest takes minutes,
so the index is rebuilt when the user *opens* the conversation, not when they ask:
``GET /sessions/{id}/messages`` schedules :func:`requeue_evicted_attachments` as a
background task, after the response. Page load, never the turn path, so it adds
nothing to time to first token.

Each evicted, still-eligible document gets one ``{"action": "index", "force":
true}`` on the large-document queue; the pipeline re-checks eligibility and the
per-user cap itself. Never raises.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from apis.shared.feature_flags import attachment_kb_enabled

logger = logging.getLogger(__name__)


def _send_all(queue_url: str, user_id: str, upload_ids: list) -> None:
    import boto3

    sqs = boto3.client("sqs", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    for upload_id in upload_ids:
        sqs.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps({"action": "index", "user_id": user_id, "upload_id": upload_id, "force": True}),
        )


async def requeue_evicted_attachments(session_id: str, user_id: str) -> int:
    """Queue a re-index for each of the caller's evicted documents in the session.

    Returns how many were queued. Rows are matched on the caller: the session
    index is keyed by conversation, not owner.
    """
    if not attachment_kb_enabled():
        return 0
    queue_url = os.environ.get("LARGE_DOCUMENT_PREPARE_QUEUE_URL", "").strip()
    if not queue_url:
        return 0
    try:
        from apis.shared.files.attachment_index import evicted_upload_ids
        from apis.shared.files.repository import get_file_upload_repository

        files = await get_file_upload_repository().list_session_files(session_id)
        upload_ids = evicted_upload_ids(f for f in files if f.user_id == user_id)
        if not upload_ids:
            return 0
        await asyncio.to_thread(_send_all, queue_url, user_id, upload_ids)
        logger.info("Re-queued %d evicted attachment(s) for indexing on conversation open", len(upload_ids))
        return len(upload_ids)
    except Exception:  # noqa: BLE001 - a re-index is never worth a failed page load
        logger.warning("Evicted attachments not re-queued", exc_info=True)
        return 0
