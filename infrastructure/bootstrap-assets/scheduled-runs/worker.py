# Bootstrap handler for the scheduled-runs worker Lambda.
#
# See the sibling dispatcher.py and the Dockerfile in this directory. A
# dispatch that lands here is dropped; the schedule's next_run_at was
# already re-armed by the dispatcher before invoking, so the schedule
# fires again on its normal cadence — self-healing, nothing is lost
# beyond one missed run.
#
# DO NOT add functionality here.

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: Any, context: Any) -> dict:
    logger.info("scheduled-runs worker bootstrap stub invoked; real image not yet deployed")
    return {"statusCode": 200, "body": "bootstrap"}
