# Bootstrap handler for the memory-maintenance worker Lambda.
#
# See the Dockerfile in this directory. A run that lands here stays queued:
# app-api reports a run that hasn't finished within its lease as failed, and
# the space's maintenance slot frees itself when the lease expires, so the
# member can start another once the real image is live.
#
# DO NOT add functionality here.

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: Any, context: Any) -> dict:
    logger.info("memory-maintenance worker bootstrap stub invoked; real image not yet deployed")
    return {"result": "bootstrap"}
