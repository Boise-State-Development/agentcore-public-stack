# Bootstrap handler for the Managed_KB migration worker Lambda.
#
# See the sibling dispatcher.py and the Dockerfile in this directory. A
# dispatch that lands here is dropped before any AWS call, so no
# knowledge base is created and no bytes are ingested; the migration
# lease expires and the dispatcher re-dispatches later — self-healing,
# nothing is lost and nothing is half-migrated.
#
# DO NOT add functionality here.

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: Any, context: Any) -> dict:
    logger.info("kb-migration worker bootstrap stub invoked; real image not yet deployed")
    return {"statusCode": 200, "body": "bootstrap"}
