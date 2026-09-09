# Bootstrap handler for the Managed_KB nightly document reconciler Lambda.
#
# See the sibling reconciler.py and the Dockerfile in this directory.
# A nightly tick that lands here reports nothing and writes nothing.
# That is the safe direction to fail: the real document reconciler only
# ever corrects a DOC# row after confirming Bedrock's own state, and it
# ships disarmed, so a run this stub missed cannot mislabel a document
# once the real image arrives.
#
# DO NOT add functionality here.

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: Any, context: Any) -> dict:
    logger.info("kb-migration document reconciler bootstrap stub invoked; real image not yet deployed")
    return {"statusCode": 200, "body": "bootstrap"}
