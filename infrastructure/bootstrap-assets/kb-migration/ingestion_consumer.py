# Bootstrap handler for the Managed_KB ingestion consumer Lambda.
#
# See the sibling dispatcher.py and the Dockerfile in this directory.
# This stub returns success, so a delivery that lands here is
# acknowledged rather than dead-lettered. That is deliberate: while the
# real image is absent the legacy ingestion pipeline is still the
# authoritative writer for every document, so there is nothing for this
# function to retry and nothing worth parking in the DLQ.
#
# DO NOT add functionality here.

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: Any, context: Any) -> dict:
    logger.info(
        "kb-migration ingestion consumer bootstrap stub invoked; real image not yet deployed"
    )
    return {"statusCode": 200, "body": "bootstrap"}
