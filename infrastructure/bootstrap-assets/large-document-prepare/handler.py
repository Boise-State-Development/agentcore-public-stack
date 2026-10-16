# Bootstrap handler for the large-document-prepare Lambda.
#
# See the Dockerfile in this directory. Like the conversation-index stub, it
# reports every message as failed rather than acknowledging it: an upload
# acknowledged here would stay `preparation = pending` with nothing to prepare
# it. Reported failures go back to the queue, are retried once the real image
# is live, and only dead-letter if it never arrives.
#
# DO NOT add functionality here.

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: Any, context: Any) -> dict:
    records = (event or {}).get("Records") or []
    logger.info(
        "large-document-prepare bootstrap stub invoked; real image not yet deployed; "
        f"returning {len(records)} message(s) to the queue"
    )
    return {
        "batchItemFailures": [
            {"itemIdentifier": r.get("messageId")} for r in records if r.get("messageId")
        ]
    }
