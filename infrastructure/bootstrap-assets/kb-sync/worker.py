# Bootstrap handler for the KB sync worker Lambda.
#
# See the sibling dispatcher.py and the Dockerfile in this directory.
# A dispatch that lands here is dropped; the policy's syncRunStartedAt
# stamp goes stale and the dispatcher re-dispatches after the stale
# window — self-healing, nothing is lost.
#
# DO NOT add functionality here.

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: Any, context: Any) -> dict:
    logger.info("kb-sync worker bootstrap stub invoked; real image not yet deployed")
    return {"statusCode": 200, "body": "bootstrap"}
