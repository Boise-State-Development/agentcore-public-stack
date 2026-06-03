# Bootstrap handler for the RAG ingestion Lambda.
#
# Same role as bootstrap-assets/artifact-render/handler.py — see
# the sibling Dockerfile in this directory and the artifact-render
# bootstrap for the rationale.
#
# RAG ingestion is invoked by S3 ObjectCreated events on the
# documents bucket. During the brief first-deploy window before
# the workflow ships the real handler, any S3 events that fire
# get routed here. We log the event and return success — the real
# handler can be re-triggered by re-uploading the document, or by
# the workflow's first deploy completing and the ingestion-on-create
# behaviour resuming for new uploads.
#
# DO NOT add functionality here.

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    logger.warning(
        'rag-ingestion bootstrap handler invoked — real handler not yet '
        'deployed. Event ignored. Re-upload the document after the '
        'backend workflow finishes. Event records: %s',
        len(event.get('Records', [])),
    )
    return {'statusCode': 503, 'body': 'rag-ingestion: bootstrap'}
