# Bootstrap handler for the Managed_KB daily reconciler Lambda.
#
# See the sibling dispatcher.py and the Dockerfile in this directory.
# A daily tick that lands here reports nothing and deletes nothing.
# That is the safe direction to fail: the reconciler age-gates orphan
# deletion on the AWS-reported `createdAt`, never on discovery time, so
# a run it missed does not turn into a run that deletes in-flight
# creates once the real image arrives.
#
# DO NOT add functionality here.

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: Any, context: Any) -> dict:
    logger.info("kb-migration reconciler bootstrap stub invoked; real image not yet deployed")
    return {"statusCode": 200, "body": "bootstrap"}
