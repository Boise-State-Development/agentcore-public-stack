# Bootstrap handler for the scheduled-runs dispatcher Lambda.
#
# Same role as the sibling bootstrap stubs — see the Dockerfile in this
# directory. During the brief first-deploy window before the workflow
# ships the real image, EventBridge ticks land here. Schedules stay due
# in the DueScheduleIndex, so the first real dispatcher tick picks them
# up — nothing is lost by no-op'ing.
#
# DO NOT add functionality here.

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: Any, context: Any) -> dict:
    logger.info("scheduled-runs dispatcher bootstrap stub invoked; real image not yet deployed")
    return {"statusCode": 200, "body": "bootstrap"}
