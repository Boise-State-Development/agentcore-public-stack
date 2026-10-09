# Bootstrap handler for the conversation-index daily reconciler Lambda.
#
# See the Dockerfile in this directory. A daily tick that lands here lists
# nothing and deletes nothing. That is the safe direction to fail: the real
# reconciler compares the index with the archive as they are on the day it
# runs, so a missed run only means its cleanup happens a day later.
#
# DO NOT add functionality here.

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: Any, context: Any) -> dict:
    logger.info("conversation-index reconciler bootstrap stub invoked; real image not yet deployed")
    return {"statusCode": 200, "body": "bootstrap"}
